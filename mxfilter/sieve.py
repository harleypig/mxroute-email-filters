"""ManageSieve access and non-destructive script editing.

Two concerns live here. ``SieveSession`` wraps ``sievelib.managesieve`` and
turns its two failure conventions -- a raised ``Error`` and a returned
``False`` -- into one exception type with a readable message. The module
functions do the offline half: parse an existing script, merge a rule into
it, and render it back, without ever discarding rules the tool did not
write.
"""

import contextlib
import datetime
import difflib
import io
import os
import re
import ssl
from collections.abc import Callable
from pathlib import Path

from sievelib import factory, parser
from sievelib.managesieve import Client
from sievelib.managesieve import Error as SieveProtocolError

from . import MxFilterError
from .config import Config

__all__ = [
    "FILTERSET_NAME",
    "MXROUTE_FORBIDDEN_ACTIONS",
    "REPORTABLE_EXTENSIONS",
    "ROUNDCUBE_NAME_MARKER",
    "SIEVELIB_NAME_MARKER",
    "UNIMPLEMENTED_ACTIONS",
    "SieveSession",
    "backup_path",
    "backup_script",
    "merge_rule",
    "parse_script",
    "remove_rule",
    "render_script",
    "resolve_backup_target",
    "rule_names",
    "script_diff",
    "write_backup",
]

# Confirmed disabled by MXRoute, from MXroute's own blog (2024-03-21):
# they "decided to disable the ability for users to create redirect sieve
# filters" because their real forwarders are designed to handle SRS
# properly. This one is a documented policy, not a capability, so it will
# not show up as a missing extension -- refusing it here is the only way to
# catch it before the server does.
#
# The message names BOTH routes deliberately. The Terraform resource is the
# as-code path and the one that will age best -- MXroute is phasing
# DirectAdmin out as a user interface, so panel-shaped instructions may go
# stale while the API-backed resource will not. But a domain that is not
# under Terraform yet has only the panel, and that reader is exactly the one
# hitting this error. Naming both costs a clause.
MXROUTE_FORBIDDEN_ACTIONS = {
    "redirect": (
        "MXRoute disables the Sieve 'redirect' action server-side (their "
        "2024-03-21 announcement). Their own forwarders are built to handle "
        "SRS correctly, which a Sieve redirect does not -- so set up a "
        "forwarder in the control panel, or with the 'mxroute_forwarder' "
        "Terraform resource, instead."
    ),
}

# NOT known to be disabled -- mxfilter simply does not generate these. No
# MXroute source says anything either way about enotify, vacation,
# extlists, or spamtest, so nothing here should claim they are unavailable.
# Whether this particular server supports one is a question its CAPABILITY
# response answers; run 'mxfilter test'.
UNIMPLEMENTED_ACTIONS = {
    "notify": "notify (enotify)",
    "vacation": "vacation",
}

# Extensions worth reporting on in 'test', purely so the answer comes from
# the server rather than from folklore. Presence or absence is discovered,
# never assumed.
REPORTABLE_EXTENSIONS = (
    "fileinto",
    "imap4flags",
    "mailbox",
    "copy",
    "envelope",
    "enotify",
    "vacation",
    "regex",
    "spamtest",
    "extlists",
)

# The name given to the in-memory filter set. It is not the script name and
# it never reaches the server -- sievelib only uses it for its own
# bookkeeping.
FILTERSET_NAME = "mxfilter"

# Two dialects name a rule in a Sieve script, and mxfilter has to read both
# and write one.
#
# `# Filter: NAME` is sievelib's, and the only one its parser recognises.
# `# rule:[NAME]` is Roundcube's managesieve plugin's -- and Roundcube is the
# webmail MXRoute actually ships, so it is the form already sitting in the
# account's active script.
#
# Read: both, because a name that reaches the parser under only one of them
# is a name that gets replaced by "Unnamed rule N" -- and the name is what
# --replace and remove-rule identify a rule by, so losing it turns "update
# the rule I named" into "append a second rule that never fires".
#
# Write: `# rule:[NAME]`. Interoperating with the webmail on the host beats
# matching the library's internal default: rules mxfilter writes stay
# visible and editable in the panel's filter UI, and rules the user wrote
# there keep their names through a merge.
ROUNDCUBE_NAME_MARKER = re.compile(r"#\s*rule:\[(?P<name>.+)\]")
SIEVELIB_NAME_MARKER = "# Filter: "


# ############################################################################
# Offline script editing
# ############################################################################


# ----------------------------------------------------------------------------
def _rewrite_hash_comments(
    text: str,
    translate: Callable[[str], str | None],
) -> str:
    """Rewrite the script's hash comments, leaving everything else alone.

    ``translate`` is handed each comment's text and returns a replacement,
    or None to leave it untouched.

    Tokenising with sievelib's own lexer -- rather than scanning lines -- is
    what makes this safe. A ``# rule:[x]`` sequence inside a quoted string,
    a ``/* ... */`` bracket comment, or a ``text:`` multi-line block is a
    different token to that lexer, so it can never be mistaken for a name
    marker. A hand-rolled line scan would have to re-derive Sieve's lexical
    rules and would disagree with the parser the moment it got one wrong.

    The work is done on the utf-8 bytes because that is what the lexer
    reports offsets in; splicing at character offsets would slide out of
    alignment on the first non-ASCII rule name.
    """
    raw = text.encode("utf-8")
    lexer = parser.Lexer(parser.Parser.lrules)
    edits: list[tuple[int, int, bytes]] = []

    try:
        for token_type, value in lexer.scan(raw):
            if token_type != "hash_comment":
                continue

            # The generator is suspended at its yield, so the lexer has not
            # advanced past the token yet and its position is the token's
            # start offset. Confirm that before splicing at it: a lexer
            # change that moved the offset would otherwise rewrite the
            # wrong bytes of the user's script, which is the one outcome
            # worse than not translating the name at all (ADR 0002).
            start = lexer.pos

            if raw[start : start + len(value)] != value:
                raise MxFilterError(
                    "cannot locate a comment in the Sieve script safely, so "
                    "rule names cannot be translated without risking the "
                    "script's contents; this is an mxfilter/sievelib "
                    "version mismatch, not a problem with your script"
                )

            comment = value.decode("utf-8")

            # ManageSieve is a CRLF protocol and the lexer's `#.*$` takes
            # the carriage return with the comment. Translate the text
            # without it, then put it back, so line endings survive.
            carriage = "\r" if comment.endswith("\r") else ""
            replacement = translate(comment[: len(comment) - len(carriage)])

            if replacement is None:
                continue

            edits.append(
                (
                    start,
                    start + len(value),
                    (replacement + carriage).encode("utf-8"),
                )
            )

    except parser.ParseError:
        # Not this function's error to report. The caller parses the same
        # text next and fails with sievelib's own diagnostic, which is the
        # hard stop ADR 0002 requires and says far more than a comment
        # rewrite could.
        return text

    if not edits:
        return text

    pieces = []
    cursor = 0

    for start, end, replacement in edits:
        pieces.append(raw[cursor:start])
        pieces.append(replacement)
        cursor = end

    pieces.append(raw[cursor:])

    return b"".join(pieces).decode("utf-8")


# ----------------------------------------------------------------------------
def _to_sievelib_names(text: str) -> str:
    """Rewrite Roundcube name markers into the form sievelib recognises."""

    def translate(comment: str) -> str | None:
        match = ROUNDCUBE_NAME_MARKER.fullmatch(comment.strip())

        if match is None:
            return None

        return f"{SIEVELIB_NAME_MARKER}{match['name']}"

    return _rewrite_hash_comments(text, translate)


# ----------------------------------------------------------------------------
def _to_roundcube_names(text: str) -> str:
    """Rewrite sievelib's name markers into Roundcube's form."""

    def translate(comment: str) -> str | None:
        stripped = comment.strip()

        if not stripped.startswith(SIEVELIB_NAME_MARKER):
            return None

        name = stripped[len(SIEVELIB_NAME_MARKER) :]

        if not name:
            return None

        return f"# rule:[{name}]"

    return _rewrite_hash_comments(text, translate)


# ----------------------------------------------------------------------------
def parse_script(text: str) -> factory.FiltersSet:
    """Parse Sieve source into an editable filter set.

    An empty or missing script is a normal starting state, not an error --
    it just yields an empty set.

    Name markers are normalised first so a rule Roundcube named arrives with
    its real name rather than as "Unnamed rule N". The rewrite is
    line-preserving, so a parse error still reports the line the user sees.
    """
    filters = factory.FiltersSet(FILTERSET_NAME)

    if not text or not text.strip():
        return filters

    script_parser = parser.Parser()

    if not script_parser.parse(_to_sievelib_names(text)):
        raise MxFilterError(
            "the existing Sieve script could not be parsed, so merging into "
            "it would risk losing rules: "
            f"{getattr(script_parser, 'error', 'unknown parse error')}"
        )

    filters.from_parser_result(script_parser)

    return filters


# ----------------------------------------------------------------------------
def render_script(filters: factory.FiltersSet) -> str:
    """Render a filter set back to Sieve source.

    Names go out in Roundcube's dialect, whichever dialect they came in as
    (see ROUNDCUBE_NAME_MARKER for why).
    """
    buffer = io.StringIO()
    filters.tosieve(buffer)

    return _to_roundcube_names(buffer.getvalue())


# ----------------------------------------------------------------------------
def rule_names(filters: factory.FiltersSet) -> list[str]:
    """Return the names of the rules in a filter set, in order."""
    return [entry["name"] for entry in filters.filters]


# ----------------------------------------------------------------------------
def merge_rule(
    existing: str,
    name: str,
    conditions: list[tuple],
    actions: list[tuple],
    matchtype: str = "anyof",
    replace: bool = False,
) -> str:
    """Merge one rule into an existing script and return the new source.

    The script is parsed and re-rendered rather than appended to, so the
    ``require`` line stays correct for the union of all rules. Every rule
    already present is carried through untouched.
    """
    filters = parse_script(existing)

    if filters.filter_exists(name):
        if not replace:
            raise MxFilterError(
                f"a rule named {name!r} already exists in the active script. "
                f"Use --replace to overwrite it, or --name to pick another."
            )

        filters.updatefilter(name, name, conditions, actions, matchtype)

    else:
        filters.addfilter(name, conditions, actions, matchtype)

    return render_script(filters)


# ----------------------------------------------------------------------------
def remove_rule(existing: str, name: str) -> str:
    """Remove a named rule and return the new script source."""
    filters = parse_script(existing)

    if not filters.removefilter(name):
        known = ", ".join(rule_names(filters)) or "(none)"

        raise MxFilterError(
            f"no rule named {name!r} in the active script. Known rules: "
            f"{known}"
        )

    return render_script(filters)


# ----------------------------------------------------------------------------
def script_diff(before: str, after: str, name: str = "sieve") -> str:
    """Return a unified diff between two script versions."""
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"{name} (current)",
        tofile=f"{name} (proposed)",
        n=3,
    )

    return "".join(lines)


# ----------------------------------------------------------------------------
def backup_path(name: str, backup_dir: Path) -> Path:
    """Return the timestamped file a backup of ``name`` would be written to.

    Separate from writing it so that ``--dry-run`` can report the path
    without creating anything, and so both callers -- the pre-upload
    backup and the ``backup`` subcommand -- derive the same name.

    The script name comes from the server, so every character that is not
    alphanumeric, ``-``, ``_``, or ``.`` becomes an underscore. That is
    what stops a name containing ``/`` or ``..`` from writing outside the
    backup directory.
    """
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")

    safe_name = "".join(
        char if char.isalnum() or char in "-_." else "_" for char in name
    )

    return Path(backup_dir) / f"{safe_name}-{stamp}.sieve"


# ----------------------------------------------------------------------------
def resolve_backup_target(
    output: str | None, name: str, backup_dir: Path
) -> Path:
    """Decide which file a backup of ``name`` is written to.

    ``output`` is the user's ``--output``; None means the default location.
    Two shapes are accepted, and which one applies is decided in this
    order:

    1. a **directory** -- a path ending in a separator, or one that already
       exists as a directory. The timestamped default filename is written
       inside it.
    2. a **full path** -- anything else. It is written exactly as given.

    The trailing separator is what lets a caller name a directory that does
    not exist yet without it being mistaken for a filename, which is the
    only case the two rules disagree about.
    """
    if output is None:
        return backup_path(name, backup_dir)

    if output.endswith(("/", os.sep)) or Path(output).is_dir():
        return backup_path(name, Path(output))

    return Path(output)


# ----------------------------------------------------------------------------
def write_backup(text: str, target: Path) -> Path:
    """Write a script to ``target`` for the owner's eyes only.

    The file is created ``0600`` and any directory made for it ``0700``. A
    Sieve script is not a credential, but it does say who the user
    corresponds with and how they sort it, so it is not world-readable
    material either.

    The bytes are the ones handed in, exactly: ``newline=""`` turns off the
    newline translation a text-mode write would otherwise apply, so a
    script the server sent with CRLF line endings comes back byte for byte.
    A backup that is not byte-identical is not a backup.
    """
    target = Path(target)

    try:
        _make_private_dir(target.parent)

        # The opener sets the mode as the file is created, so it is never
        # briefly world-readable; the chmod afterwards covers the case
        # where the file already existed, when the create mode is ignored.
        def private(path, flags):
            return os.open(path, flags, 0o600)

        with open(
            target, "w", encoding="utf-8", newline="", opener=private
        ) as stream:
            stream.write(text)

        os.chmod(target, 0o600)

    except OSError as exc:
        raise MxFilterError(
            f"could not write backup to {target}: {exc}"
        ) from exc

    return target


# ----------------------------------------------------------------------------
def _make_private_dir(directory: Path) -> None:
    """Create ``directory`` and any missing parent, mode ``0700``.

    ``mkdir(parents=True)`` applies its mode to the leaf only -- every
    parent it creates gets the process umask instead -- so the directories
    that did not exist are collected first and chmod'ed afterwards.
    Directories that were already there are left exactly as the user set
    them; this only decides the mode of what mxfilter itself creates.
    """
    created = []
    probe = directory

    while not probe.exists():
        created.append(probe)
        probe = probe.parent

    directory.mkdir(parents=True, exist_ok=True)

    for made in created:
        made.chmod(0o700)


# ----------------------------------------------------------------------------
def backup_script(text: str, name: str, backup_dir: Path) -> Path:
    """Write the current script to a timestamped file and return its path.

    Taken before every upload, and by ``mxfilter backup`` when no
    ``--output`` says otherwise. Restoring is then a plain ``mxfilter``
    -free operation: the file is the exact bytes the server had.
    """
    return write_backup(text, backup_path(name, backup_dir))


# ############################################################################
# Server session
# ############################################################################


class SieveSession:
    """A connected ManageSieve client with human-readable failures."""

    # ------------------------------------------------------------------------
    def __init__(
        self,
        config: Config,
        progress: Callable[[str], None] | None = None,
    ):
        """Record the settings; no connection is made until ``open()``.

        ``progress`` receives step-by-step messages. It is a callback rather
        than a print so this module stays free of presentation -- the CLI
        passes a printer under ``--verbose``, and anything else can route
        the same messages to a status bar or a log.
        """
        self.config = config
        self.progress = progress
        self.client: Client | None = None

    # ------------------------------------------------------------------------
    def __enter__(self) -> "SieveSession":
        self.open()

        return self

    # ------------------------------------------------------------------------
    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.close()

        return False

    # ------------------------------------------------------------------------
    def _log(self, message: str) -> None:
        """Hand a progress message to the caller's callback, if any."""
        if self.progress is not None:
            self.progress(message)

    # ------------------------------------------------------------------------
    def open(self) -> None:
        """Connect and authenticate.

        sievelib's ``debug=True`` echoes the raw protocol, including the
        base64 AUTHENTICATE payload that carries the password. There is no
        safe way to expose that as a flag, so it is pinned off here.
        """
        config = self.config
        config.require("host", "user")

        self._log(
            f"connecting to {config.host}:{config.sieve_port} "
            f"(tls={config.sieve_tls}) as {config.user}"
        )

        client = Client(config.host, config.sieve_port, debug=False)

        try:
            authenticated = client.connect(
                config.user,
                config.password().reveal(),
                starttls=(config.sieve_tls == "starttls"),
                ssl=(config.sieve_tls == "ssl"),
            )

        except SieveProtocolError as exc:
            raise MxFilterError(
                f"ManageSieve error talking to {config.host}:"
                f"{config.sieve_port} -- {exc}. "
                f"{_connection_hint(config)}"
            ) from exc

        except ssl.SSLError as exc:
            raise MxFilterError(
                f"TLS failure against {config.host}:{config.sieve_port} -- "
                f"{exc}. {_connection_hint(config)}"
            ) from exc

        except OSError as exc:
            raise MxFilterError(
                f"cannot reach {config.host}:{config.sieve_port} -- {exc}. "
                f"{_connection_hint(config)}"
            ) from exc

        if not authenticated:
            raise MxFilterError(
                f"ManageSieve authentication failed for {config.user!r} "
                f"(password {config.password_state()}). MXRoute expects the "
                f"FULL email address as the username, e.g. "
                f"you@yourdomain.com."
            )

        self.client = client
        self._log("authenticated")

    # ------------------------------------------------------------------------
    def close(self) -> None:
        """Log out, ignoring a connection that has already gone away."""
        if self.client is None:
            return

        with contextlib.suppress(SieveProtocolError, OSError):
            self.client.logout()

        self.client = None

    # ------------------------------------------------------------------------
    def _require_client(self) -> Client:
        """Return the live client or fail loudly."""
        if self.client is None:
            raise MxFilterError("ManageSieve session is not open")

        return self.client

    # ------------------------------------------------------------------------
    def capabilities(self) -> list[str]:
        """Return the Sieve extensions the server advertises."""
        client = self._require_client()

        try:
            return list(client.get_sieve_capabilities())

        except (KeyError, TypeError):
            return []

    # ------------------------------------------------------------------------
    def missing_extensions(self, required: set[str]) -> list[str]:
        """Return the requested extensions the server does not advertise."""
        advertised = {name.lower() for name in self.capabilities()}

        return sorted(
            name for name in required if name.lower() not in advertised
        )

    # ------------------------------------------------------------------------
    def list_scripts(self) -> tuple[str | None, list[str]]:
        """Return ``(active_script, other_scripts)``."""
        client = self._require_client()

        try:
            result = client.listscripts()

        except SieveProtocolError as exc:
            raise MxFilterError(f"LISTSCRIPTS failed -- {exc}") from exc

        if result is None:
            return (None, [])

        active, others = result

        return (active, list(others or []))

    # ------------------------------------------------------------------------
    def active_script_name(self) -> str | None:
        """Return the active script's name, or None if none is active."""
        active, _others = self.list_scripts()

        return active

    # ------------------------------------------------------------------------
    def get_script(self, name: str) -> str:
        """Fetch one script's source."""
        client = self._require_client()

        try:
            content = client.getscript(name)

        except SieveProtocolError as exc:
            raise MxFilterError(f"GETSCRIPT {name!r} failed -- {exc}") from exc

        if content is False or content is None:
            raise MxFilterError(f"could not download script {name!r}")

        return content

    # ------------------------------------------------------------------------
    def check_script(self, content: str) -> None:
        """Ask the server to validate a script before uploading it."""
        client = self._require_client()
        self._log("validating script with CHECKSCRIPT")

        try:
            accepted = client.checkscript(content)

        except SieveProtocolError as exc:
            raise MxFilterError(
                f"the server rejected the generated script -- {exc}"
            ) from exc

        if not accepted:
            raise MxFilterError(
                "the server rejected the generated script (CHECKSCRIPT "
                "returned failure); nothing was uploaded"
            )

    # ------------------------------------------------------------------------
    def put_script(self, name: str, content: str) -> None:
        """Upload a script, replacing any script of the same name."""
        client = self._require_client()
        self._log(f"uploading script {name!r} ({len(content)} bytes)")

        try:
            stored = client.putscript(name, content)

        except SieveProtocolError as exc:
            raise MxFilterError(f"PUTSCRIPT {name!r} failed -- {exc}") from exc

        if not stored:
            raise MxFilterError(f"PUTSCRIPT {name!r} failed")

    # ------------------------------------------------------------------------
    def set_active(self, name: str) -> None:
        """Make a script the active one."""
        client = self._require_client()
        self._log(f"activating script {name!r}")

        try:
            activated = client.setactive(name)

        except SieveProtocolError as exc:
            raise MxFilterError(f"SETACTIVE {name!r} failed -- {exc}") from exc

        if not activated:
            raise MxFilterError(f"SETACTIVE {name!r} failed")


# ----------------------------------------------------------------------------
def _connection_hint(config: Config) -> str:
    """Return a hint tuned to the port and TLS mode that failed.

    MXRoute documents neither a ManageSieve port nor whether it speaks
    STARTTLS or implicit TLS. 4190 is the IANA-registered port (RFC 5804)
    and the Dovecot default, which makes it the right default and not a
    verified fact -- so a failure has to say that plainly instead of
    implying the user mistyped something.
    """
    hints = [
        f"MXRoute does not publish its ManageSieve port or TLS mode; "
        f"{config.sieve_port} + {config.sieve_tls} is the RFC 5804 / Dovecot "
        f"default, not a documented MXRoute setting."
    ]

    if config.sieve_tls == "starttls":
        hints.append("Try --sieve-tls ssl (implicit TLS) as the alternative.")

    else:
        hints.append("Try --sieve-tls starttls as the alternative.")

    hints.append(
        "Also confirm the hostname (the panel's Email Clients page shows it; "
        "it is per-account, the same as your primary MX record), that "
        f"outbound {config.sieve_port} is not blocked, and if all else "
        f"fails ask MXRoute support which port and TLS mode to use."
    )

    return " ".join(hints)

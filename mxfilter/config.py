"""Endpoint and credential resolution.

Resolution order for every setting, highest priority first:

1. a CLI flag
2. an environment variable (``MXROUTE_*``)
3. the TOML config file (``$XDG_CONFIG_HOME/mxfilter/config.toml``)
4. a built-in default

The password is handled separately -- it has more than one source and its
own ladder (``Config.password``) -- and never lands in a plain string that
could be printed by accident -- see the ``Secret`` class below.
"""

import os
import shlex
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import MxFilterError

__all__ = [
    "Config",
    "Secret",
    "config_dir",
    "config_path",
    "default_backup_dir",
    "load_config",
]

DEFAULT_SIEVE_PORT = 4190
DEFAULT_IMAP_PORT = 993
DEFAULT_SIEVE_TLS = "starttls"
DEFAULT_SOURCE_FOLDER = "INBOX"

SIEVE_TLS_MODES = ("starttls", "ssl", "none")

# Every way of supplying a password, in one message, because a user who
# sees this has just found out that none of them is in place.
NO_PASSWORD_MESSAGE = (
    "no password available -- pass --password-file, --password-cmd, or "
    "--password, set MXROUTE_PASSWORD_FILE, MXROUTE_PASSWORD_CMD, or "
    "MXROUTE_PASSWORD, or put password_file / password_cmd in the config "
    "file"
)

# What password_state() reports for each kind of source. A literal from a
# flag and one from the environment are the same kind of value and are
# resolved identically; they are labelled apart only so `mxfilter test` can
# say which one is in play. No label says anything about the value itself.
PASSWORD_STATE_LABELS = {
    "file": "set (via file)",
    "command": "set (via command)",
    "flag": "set (via flag)",
    "env": "set",
}


# ############################################################################
# Secret handling
# ############################################################################


class Secret:
    """A password that refuses to render itself.

    Every accidental path to disclosure -- ``print``, an f-string, ``repr``
    in a traceback frame, ``%s`` in a log line -- goes through ``__str__``
    or ``__repr__``, so overriding both turns the whole class of mistakes
    into a harmless ``<redacted>``. The real value is reachable only by
    calling ``reveal()``, which is greppable and therefore reviewable.
    """

    __slots__ = ("_value",)

    # ------------------------------------------------------------------------
    def __init__(self, value: str):
        """Wrap ``value``; it is never copied anywhere else."""
        self._value = value

    # ------------------------------------------------------------------------
    def reveal(self) -> str:
        """Return the wrapped value. Call this only when handing it to a
        connection method -- never to display, log, or format it."""
        return self._value

    # ------------------------------------------------------------------------
    def __str__(self) -> str:
        return "<redacted>"

    # ------------------------------------------------------------------------
    def __repr__(self) -> str:
        return "<Secret redacted>"

    # ------------------------------------------------------------------------
    def __bool__(self) -> bool:
        return bool(self._value)


# ############################################################################
# Config
# ############################################################################


@dataclass
class Config:
    """Resolved connection settings for one MXRoute account."""

    host: str = ""
    user: str = ""
    imap_host: str = ""
    imap_port: int = DEFAULT_IMAP_PORT
    sieve_port: int = DEFAULT_SIEVE_PORT
    sieve_tls: str = DEFAULT_SIEVE_TLS
    default_folder: str = ""
    source_folder: str = DEFAULT_SOURCE_FOLDER
    backup_dir: Path = field(default_factory=lambda: default_backup_dir())

    # The three credential flags. argparse makes them mutually exclusive,
    # so at most one is ever populated from the command line: they are
    # three ways of saying the same explicit thing, and ranking equally
    # explicit instructions against each other would be a rule to
    # remember rather than a rule to apply.
    #
    # ``--password`` lands in ``inline_password`` rather than ``password``
    # because ``password()`` is the method that resolves the credential,
    # and a field of that name would shadow it.
    inline_password: str = field(default="", repr=False)
    password_file: str = ""
    password_cmd: str = ""

    # The same two settings as read from the TOML file, kept apart from
    # the flags because they sit at a different height in the ladder: a
    # flag beats the environment and the config file does not. Merging
    # them into one field is what inverted the order previously.
    toml_password_file: str = ""
    toml_password_cmd: str = ""

    # Supplied by the front-end (the CLI passes getpass). Left None by
    # anything that cannot prompt, which then gets a clean error instead of
    # a process blocked on a terminal read that will never be answered.
    prompter: Callable[[str], str] | None = field(default=None, repr=False)

    # Resolved lazily by password(); never populated from a repr-able place.
    _password: Secret | None = field(default=None, repr=False)

    # ------------------------------------------------------------------------
    def __repr__(self) -> str:
        """Render without the credential.

        The dataclass-generated repr would happily print ``_password``; a
        Config lands in tracebacks and debug dumps, so the credential state
        is reported as a literal instead of a value.
        """
        return (
            f"Config(host={self.host!r}, user={self.user!r}, "
            f"imap_host={self.imap_host!r}, imap_port={self.imap_port!r}, "
            f"sieve_port={self.sieve_port!r}, sieve_tls={self.sieve_tls!r}, "
            f"default_folder={self.default_folder!r}, "
            f"source_folder={self.source_folder!r}, "
            f"password={self.password_state()})"
        )

    # ------------------------------------------------------------------------
    def password_sources(self) -> list[tuple[str, str]]:
        """Return the configured password sources, highest priority first.

        Each entry is ``(kind, value)``, where the kind says how to turn
        the value into a credential -- read a file, run a command, or take
        it literally -- and the position says which one wins.

        The ladder, and why it is in this order:

        1. an explicit flag (``--password-file``, ``--password-cmd``,
           ``--password``; mutually exclusive, so only one can appear)
        2. ``MXROUTE_PASSWORD_FILE``
        3. ``MXROUTE_PASSWORD_CMD``
        4. ``MXROUTE_PASSWORD``
        5. ``password_file`` from the config file
        6. ``password_cmd`` from the config file

        A flag outranks an ambient variable because it was typed for this
        run and the variable was not. The failure that ordering prevents
        is not an inconvenience: with ``MXROUTE_PASSWORD`` exported for one
        account, a ``--password-cmd`` naming a *second* account used to be
        ignored, and the command authenticated as the first -- the wrong
        account, with no error anywhere.

        Within the flags the order is nominal, since argparse rejects more
        than one; it runs safest-first so a Config assembled by hand
        (a test, another front-end) still behaves sensibly.
        """
        env = os.environ

        candidates = [
            ("file", self.password_file),
            ("command", self.password_cmd),
            ("flag", self.inline_password),
            ("file", env.get("MXROUTE_PASSWORD_FILE", "")),
            ("command", env.get("MXROUTE_PASSWORD_CMD", "")),
            ("env", env.get("MXROUTE_PASSWORD", "")),
            ("file", self.toml_password_file),
            ("command", self.toml_password_cmd),
        ]

        return [(kind, value) for kind, value in candidates if value]

    # ------------------------------------------------------------------------
    def password_state(self) -> str:
        """Report whether a credential is available, never what it is.

        Every return value is a fixed literal chosen from
        ``PASSWORD_STATE_LABELS``; none is derived from the credential, so
        this is safe to print, log, and put in an error message.
        """
        if self._password is not None:
            return "set"

        sources = self.password_sources()

        if not sources:
            return "unset"

        kind, _value = sources[0]

        return PASSWORD_STATE_LABELS[kind]

    # ------------------------------------------------------------------------
    def password(self) -> Secret:
        """Resolve the password, asking the prompter only as a last resort.

        The order is ``password_sources()``; the prompt is the last rung
        below all of them. Resolution is deferred until a connection is
        actually opened so that ``--help`` and the offline code paths
        never trigger a prompt or run a credential command.

        The interactive prompt itself is *not* implemented here: a core
        module must not own a terminal interaction, or a non-terminal
        front-end could never reuse it. The caller supplies ``prompter``
        (the CLI passes ``getpass``); with none set, the absence of a
        credential is simply an error.
        """
        if self._password is not None:
            return self._password

        sources = self.password_sources()

        if sources:
            self._password = self._resolve_source(*sources[0])

        elif self.prompter is not None:
            self._password = Secret(
                self.prompter(f"Password for {self.user or 'account'}: ")
            )

        else:
            raise MxFilterError(NO_PASSWORD_MESSAGE)

        if not self._password:
            raise MxFilterError(NO_PASSWORD_MESSAGE)

        return self._password

    # ------------------------------------------------------------------------
    def _resolve_source(self, kind: str, value: str) -> Secret:
        """Turn one ``(kind, value)`` source into a credential."""
        if kind == "file":
            return read_password_file(Path(value))

        if kind == "command":
            return run_password_command(value)

        return Secret(value)

    # ------------------------------------------------------------------------
    def require(self, *names: str) -> None:
        """Fail with a single actionable message if a setting is missing."""
        missing = [name for name in names if not getattr(self, name, None)]

        if not missing:
            return

        hints = ", ".join(f"--{name.replace('_', '-')}" for name in missing)

        raise MxFilterError(
            f"missing required setting(s): {', '.join(missing)}. "
            f"Set {hints}, the matching MXROUTE_* variable, or add it to "
            f"{config_path()}"
        )


# ############################################################################
# Loading
# ############################################################################


# ----------------------------------------------------------------------------
def config_dir() -> Path:
    """Return mxfilter's own directory, honouring ``XDG_CONFIG_HOME``."""
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"

    return Path(base) / "mxfilter"


# ----------------------------------------------------------------------------
def config_path() -> Path:
    """Return the TOML config location, honouring ``XDG_CONFIG_HOME``."""
    return config_dir() / "config.toml"


# ----------------------------------------------------------------------------
def default_backup_dir() -> Path:
    """Return where script backups are written by default.

    This is the **config** directory, not the state directory. XDG would
    call a backup state -- it is machine-generated data the program can
    recreate, not something the user edits -- and putting it here is a
    deliberate departure from that, not something XDG endorses.

    The reason is that a backup the user cannot find is not a backup. The
    config directory is the one mxfilter path a user already knows, having
    put ``config.toml`` there; ``~/.local/state`` is a path most people
    have never opened, and the moment it matters is the moment a script
    has just been mangled and nobody wants to go looking. Co-locating also
    keeps ``mxfilter backup`` and the automatic pre-upload backup in one
    place instead of two.

    ``MXROUTE_BACKUP_DIR`` / ``backup_dir`` override it either way.
    """
    return config_dir() / "backups"


# ----------------------------------------------------------------------------
def read_config_file(path: Path) -> dict:
    """Parse the TOML config file, tolerating its absence."""
    if not path.is_file():
        return {}

    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)

    except tomllib.TOMLDecodeError as exc:
        raise MxFilterError(f"{path}: invalid TOML -- {exc}") from exc

    except OSError as exc:
        raise MxFilterError(f"{path}: cannot read -- {exc}") from exc


# ----------------------------------------------------------------------------
def check_password_file_mode(path: Path) -> None:
    """Refuse a password file that group or other can reach.

    Any bit in ``0o077`` means somebody other than the owner can read the
    credential, so ``0600`` and ``0400`` pass and ``0640``, ``0604``,
    ``0644`` and the rest do not. The owner's own bits are not our
    business; only shared access is.

    The check runs before the file is opened, so a file with a bad mode is
    never read at all. ``libpq`` treats ``~/.pgpass`` the same way, except
    that it silently ignores the file; this names it and stops, because
    here the file was asked for by name and ignoring it would send the
    caller down the rest of the ladder without saying so.
    """
    try:
        mode = path.stat().st_mode & 0o777

    except OSError as exc:
        raise MxFilterError(
            f"{path}: cannot read password file -- {exc}"
        ) from exc

    if not mode & 0o077:
        return

    raise MxFilterError(
        f"password file {path} is readable by group/other (mode {mode:04o}); "
        f"mxfilter refuses to read it. Fix with: chmod 600 {path}"
    )


# ----------------------------------------------------------------------------
def read_password_file(path: Path) -> Secret:
    """Read a password from a file, checking its mode first.

    Exactly one trailing newline is removed, the one every editor adds,
    and nothing else. Trailing spaces are left alone, because a space can
    be part of a password and eating it silently produces an
    authentication failure with no visible cause.

    A file that holds nothing but whitespace is refused rather than
    treated as a password of spaces: it is the shape an empty or
    half-written file takes, and the same unexplainable auth failure is
    the alternative.

    No error here quotes the file's contents, only its path.
    """
    check_password_file_mode(path)

    try:
        raw = path.read_text(encoding="utf-8")

    except OSError as exc:
        raise MxFilterError(
            f"{path}: cannot read password file -- {exc}"
        ) from exc

    except UnicodeDecodeError as exc:
        raise MxFilterError(
            f"{path}: password file is not valid UTF-8 -- {exc.reason}"
        ) from exc

    value = strip_one_newline(raw)

    if not value.strip():
        raise MxFilterError(f"{path}: password file is empty")

    return Secret(value)


# ----------------------------------------------------------------------------
def strip_one_newline(text: str) -> str:
    """Remove a single trailing ``\\n`` or ``\\r\\n``, and nothing else."""
    if text.endswith("\r\n"):
        return text[:-2]

    if text.endswith("\n"):
        return text[:-1]

    return text


# ----------------------------------------------------------------------------
def run_password_command(command: str) -> Secret:
    """Run a credential command and capture its first stdout line.

    Split with ``shlex`` and run without a shell so the password can never
    be re-expanded by one. On failure only the exit status and the program
    name are reported: a credential helper's stderr is not a safe thing to
    echo, since it may quote the value it was asked for.
    """
    argv = shlex.split(command)

    if not argv:
        raise MxFilterError("password command is empty")

    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, check=False
        )

    except OSError as exc:
        raise MxFilterError(
            f"password command {argv[0]!r} could not be run -- {exc}"
        ) from exc

    if completed.returncode != 0:
        raise MxFilterError(
            f"password command {argv[0]!r} failed with exit "
            f"{completed.returncode} (its output is not shown, as it may "
            f"contain the credential)"
        )

    value = completed.stdout.split("\n", 1)[0].strip()

    if not value:
        raise MxFilterError(f"password command {argv[0]!r} produced no output")

    return Secret(value)


# ----------------------------------------------------------------------------
def _pick(*candidates, default=None):
    """Return the first candidate that is neither None nor empty."""
    for candidate in candidates:
        if candidate not in (None, ""):
            return candidate

    return default


# ----------------------------------------------------------------------------
def _as_port(value, label: str) -> int:
    """Coerce a port to int with a message naming which port failed."""
    try:
        return int(value)

    except (TypeError, ValueError) as exc:
        raise MxFilterError(
            f"{label}: {value!r} is not a port number"
        ) from exc


# ----------------------------------------------------------------------------
def load_config(args) -> Config:
    """Build a Config from CLI args, environment, and the config file.

    ``args`` is the parsed argparse namespace; any of the connection
    attributes may be absent or None, which simply defers to the next
    source in the resolution order.
    """
    file_values = read_config_file(config_path())
    env = os.environ

    def flag(name):
        return getattr(args, name, None)

    host = _pick(
        flag("host"),
        env.get("MXROUTE_HOST"),
        file_values.get("host"),
        default="",
    )

    user = _pick(
        flag("user"),
        env.get("MXROUTE_USER"),
        file_values.get("user"),
        default="",
    )

    imap_host = _pick(
        flag("imap_host"),
        env.get("MXROUTE_IMAP_HOST"),
        file_values.get("imap_host"),
        host,
        default="",
    )

    imap_port = _as_port(
        _pick(
            flag("imap_port"),
            env.get("MXROUTE_IMAP_PORT"),
            file_values.get("imap_port"),
            default=DEFAULT_IMAP_PORT,
        ),
        "imap_port",
    )

    sieve_port = _as_port(
        _pick(
            flag("sieve_port"),
            env.get("MXROUTE_SIEVE_PORT"),
            file_values.get("sieve_port"),
            default=DEFAULT_SIEVE_PORT,
        ),
        "sieve_port",
    )

    sieve_tls = _pick(
        flag("sieve_tls"),
        env.get("MXROUTE_SIEVE_TLS"),
        file_values.get("sieve_tls"),
        default=DEFAULT_SIEVE_TLS,
    )

    if sieve_tls not in SIEVE_TLS_MODES:
        raise MxFilterError(
            f"sieve_tls: {sieve_tls!r} is not one of "
            f"{', '.join(SIEVE_TLS_MODES)}"
        )

    backup_dir = _pick(
        flag("backup_dir"),
        env.get("MXROUTE_BACKUP_DIR"),
        file_values.get("backup_dir"),
        default=None,
    )

    return Config(
        host=host,
        user=user,
        imap_host=imap_host,
        imap_port=imap_port,
        sieve_port=sieve_port,
        sieve_tls=sieve_tls,
        default_folder=_pick(file_values.get("default_folder"), default=""),
        source_folder=_pick(
            flag("folder"),
            file_values.get("source_folder"),
            default=DEFAULT_SOURCE_FOLDER,
        ),
        # Four fields rather than two: which source a credential came from
        # is what decides the order, so collapsing a flag and a config-file
        # value into one field would throw the answer away before
        # password_sources() is ever asked the question.
        inline_password=_pick(flag("password"), default=""),
        password_file=_pick(flag("password_file"), default=""),
        password_cmd=_pick(flag("password_cmd"), default=""),
        toml_password_file=_pick(file_values.get("password_file"), default=""),
        toml_password_cmd=_pick(file_values.get("password_cmd"), default=""),
        backup_dir=Path(backup_dir) if backup_dir else default_backup_dir(),
    )

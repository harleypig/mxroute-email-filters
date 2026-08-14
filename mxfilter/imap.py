"""IMAP access for the existing-mail pass.

Wraps IMAPClient with the three things the CLI actually needs: the folder
list plus its hierarchy delimiter, a search that is re-checked against the
real Sieve comparison semantics, and a move that degrades gracefully when
the server has no MOVE capability.

Folder naming is the reason the delimiter matters. A Maildir++ layout --
which MXRoute's published maildir paths suggest, though nothing documents
it -- spells a subfolder ``INBOX.Lists.GitHub``, while other layouts spell
the same thing ``Lists/GitHub``. Users should be able to type either, so
every folder name is normalized against the delimiter the server actually
reports. That detection is mandatory, not an optimization: guessing wrong
files mail into a folder nobody reads.

The same trap applies to the spam folder, which on MXRoute is
``INBOX.spam`` in lower case. Filing into ``Junk`` would silently create a
second folder next to the real one, so folder names are matched against the
server's own list (case-insensitively) rather than taken on trust.
"""

import contextlib
import email
import socket
import ssl
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from email.header import decode_header, make_header

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError, LoginError

from . import MxFilterError
from .config import Config
from .criteria import Criteria

__all__ = [
    "ImapSession",
    "MailActionPlan",
    "MailActionResult",
    "MessageSummary",
    "decode_header_value",
    "normalize_folder",
    "split_path",
]


# ############################################################################
# Folder naming
# ############################################################################


# ----------------------------------------------------------------------------
def split_path(name: str, delimiter: str) -> list[str]:
    """Split a user-supplied folder name into its components.

    Both ``/`` and the server's own delimiter are accepted as separators so
    that ``Lists/GitHub`` and ``INBOX.Lists.GitHub`` describe the same
    folder on a Maildir++ server.
    """
    separators = {"/", delimiter}
    parts = [name]

    for separator in separators:
        if not separator:
            continue

        parts = [piece for part in parts for piece in part.split(separator)]

    return [part for part in parts if part]


# ----------------------------------------------------------------------------
def normalize_folder(
    name: str, delimiter: str, known: list[str] | None = None
) -> str:
    """Return the server's spelling of a user-supplied folder name.

    When the folder list is available the answer is looked up rather than
    guessed, which also matches an existing folder whose case differs. The
    fallback only kicks in for a folder that does not exist yet: on a
    Maildir++ server (delimiter ``.``) a new folder belongs under ``INBOX``,
    while a ``/``-delimited server keeps it as a top-level sibling.
    """
    components = split_path(name, delimiter)

    if not components:
        raise MxFilterError("empty folder name")

    candidate = delimiter.join(components)

    if components[0].upper() == "INBOX":
        candidate = delimiter.join(["INBOX", *components[1:]])

    if known:
        lookup = {folder.casefold(): folder for folder in known}

        for option in (candidate, f"INBOX{delimiter}{candidate}"):
            match = lookup.get(option.casefold())

            if match:
                return match

    if delimiter == "." and components[0].upper() != "INBOX":
        return delimiter.join(["INBOX", *components])

    return candidate


# ############################################################################
# Messages
# ############################################################################


@dataclass(frozen=True)
class MessageSummary:
    """One matched message, as data.

    Deliberately carries no rendering of itself: how a message is displayed
    is the front-end's business, and a record that knows how to draw itself
    is the thing a second front-end has to work around.
    """

    uid: int
    date: str
    sender: str
    subject: str
    folder: str


@dataclass(frozen=True)
class MailActionPlan:
    """What the existing-mail pass would do, worked out but not yet done.

    Produced by a read-only search, so building a plan is always safe. The
    caller decides whether to render it, confirm it, or execute it -- which
    is what makes a dry run a matter of not calling ``execute``, rather than
    of a flag changing what some deeper function prints.
    """

    source: str
    destination: str
    flags: list[str]
    discard: bool
    messages: list[MessageSummary] = field(default_factory=list)

    # ------------------------------------------------------------------------
    @property
    def count(self) -> int:
        """How many messages the plan covers."""
        return len(self.messages)

    # ------------------------------------------------------------------------
    @property
    def uids(self) -> list[int]:
        """The UIDs the plan would act on."""
        return [message.uid for message in self.messages]

    # ------------------------------------------------------------------------
    @property
    def moves(self) -> bool:
        """Whether executing this plan relocates mail."""
        return bool(
            self.destination
            and not self.discard
            and self.destination.casefold() != self.source.casefold()
        )

    # ------------------------------------------------------------------------
    @property
    def is_empty(self) -> bool:
        """Whether the plan would do nothing at all."""
        return not self.messages


@dataclass(frozen=True)
class MailActionResult:
    """What executing a plan actually did."""

    flagged: int = 0
    moved: int = 0
    deleted: int = 0


# ----------------------------------------------------------------------------
def decode_header_value(raw: str) -> str:
    """Decode RFC 2047 encoded words, falling back to the raw value."""
    try:
        return str(make_header(decode_header(raw)))

    except (UnicodeDecodeError, LookupError, ValueError):
        return raw


# ----------------------------------------------------------------------------
def header_values(message) -> dict[str, list[str]]:
    """Map upper-cased header names to every occurrence of that header.

    Each occurrence contributes both its decoded and its raw form. Sieve
    compares against the MIME-decoded value, so that is the one that
    matters; keeping the raw form as well means a search for the literal
    encoded text still finds its message, and costs only a wider candidate
    set.
    """
    collected: dict[str, list[str]] = {}

    for name, raw in message.items():
        key = name.upper()
        decoded = decode_header_value(raw)

        values = collected.setdefault(key, [])
        values.append(decoded)

        if decoded != raw:
            values.append(raw)

    return collected


# ############################################################################
# Session
# ############################################################################


class ImapSession:
    """A connected IMAP client scoped to one account."""

    # ------------------------------------------------------------------------
    def __init__(
        self,
        config: Config,
        progress: Callable[[str], None] | None = None,
    ):
        """Record the settings; no connection is made until ``open()``.

        ``progress`` receives step-by-step messages, as a callback rather
        than a print so this module carries no presentation of its own.
        """
        self.config = config
        self.progress = progress
        self.client: IMAPClient | None = None
        self._delimiter = "."
        self._folders: list[str] = []

    # ------------------------------------------------------------------------
    def __enter__(self) -> "ImapSession":
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
        """Connect, authenticate, and read the folder list."""
        config = self.config
        config.require("imap_host", "user")

        use_ssl = config.imap_port != 143

        self._log(
            f"connecting to {config.imap_host}:{config.imap_port} "
            f"(ssl={use_ssl}) as {config.user}"
        )

        try:
            client = IMAPClient(
                config.imap_host, port=config.imap_port, ssl=use_ssl
            )

            if not use_ssl:
                client.starttls()

            client.login(config.user, config.password().reveal())

        except LoginError as exc:
            raise MxFilterError(
                f"IMAP authentication failed for {config.user!r} (password "
                f"{config.password_state()}). MXRoute expects the FULL "
                f"email address as the username, e.g. you@yourdomain.com. "
                f"[{exc}]"
            ) from exc

        except ssl.SSLError as exc:
            raise MxFilterError(
                f"TLS failure against {config.imap_host}:{config.imap_port} "
                f"-- {exc}. Port 993 is implicit TLS; port 143 uses STARTTLS."
            ) from exc

        except (socket.gaierror, OSError) as exc:
            raise MxFilterError(
                f"cannot reach {config.imap_host}:{config.imap_port} -- "
                f"{exc}. Check --imap-host and --imap-port."
            ) from exc

        except IMAPClientError as exc:
            raise MxFilterError(f"IMAP error -- {exc}") from exc

        self.client = client
        self._read_folders()
        self._log(
            f"connected; delimiter {self._delimiter!r}, "
            f"{len(self._folders)} folders"
        )

    # ------------------------------------------------------------------------
    def close(self) -> None:
        """Log out, ignoring a connection that has already gone away."""
        if self.client is None:
            return

        with contextlib.suppress(IMAPClientError, OSError):
            self.client.logout()

        self.client = None

    # ------------------------------------------------------------------------
    def _require_client(self) -> IMAPClient:
        """Return the live client or fail loudly."""
        if self.client is None:
            raise MxFilterError("IMAP session is not open")

        return self.client

    # ------------------------------------------------------------------------
    def _read_folders(self) -> None:
        """Cache the folder list and the hierarchy delimiter."""
        client = self._require_client()

        try:
            listing = client.list_folders()

        except IMAPClientError as exc:
            raise MxFilterError(f"LIST failed -- {exc}") from exc

        folders = []

        for _flags, delimiter, name in listing:
            if delimiter:
                self._delimiter = (
                    delimiter.decode()
                    if isinstance(delimiter, bytes)
                    else str(delimiter)
                )

            folders.append(
                name.decode() if isinstance(name, bytes) else str(name)
            )

        self._folders = folders

    # ------------------------------------------------------------------------
    @property
    def delimiter(self) -> str:
        """The server's folder hierarchy delimiter."""
        return self._delimiter

    # ------------------------------------------------------------------------
    @property
    def folders(self) -> list[str]:
        """Every folder the account can see."""
        return list(self._folders)

    # ------------------------------------------------------------------------
    def capabilities(self) -> list[str]:
        """Return the advertised IMAP capabilities as strings."""
        client = self._require_client()

        return [
            item.decode() if isinstance(item, bytes) else str(item)
            for item in client.capabilities()
        ]

    # ------------------------------------------------------------------------
    def normalize(self, name: str) -> str:
        """Normalize a folder name against this server's naming."""
        return normalize_folder(name, self._delimiter, self._folders)

    # ------------------------------------------------------------------------
    def exists(self, folder: str) -> bool:
        """Whether a (already normalized) folder exists."""
        return any(
            candidate.casefold() == folder.casefold()
            for candidate in self._folders
        )

    # ------------------------------------------------------------------------
    def create_folder(self, folder: str) -> None:
        """Create a folder and refresh the cached listing."""
        client = self._require_client()
        self._log(f"creating folder {folder!r}")

        try:
            client.create_folder(folder)

        except IMAPClientError as exc:
            raise MxFilterError(
                f"could not create folder {folder!r} -- {exc}"
            ) from exc

        self._read_folders()

    # ------------------------------------------------------------------------
    def search(
        self, criteria: Criteria, folder: str, readonly: bool = True
    ) -> list[MessageSummary]:
        """Return the messages in ``folder`` that really match ``criteria``.

        The IMAP search narrows the mailbox; the fetched headers then decide.
        That second pass is not belt-and-braces -- IMAP can only substring
        match, so it is the only thing that makes ``--compare is`` and
        ``--compare matches`` mean the same here as they will in Sieve.
        """
        client = self._require_client()
        self._select(folder, readonly=readonly)

        key = criteria.imap_search_key()
        self._log(f"searching {folder!r} with {key}")

        try:
            uids = client.search(key)

        except IMAPClientError as exc:
            raise MxFilterError(
                f"IMAP search in {folder!r} failed -- {exc}"
            ) from exc

        if not uids:
            return []

        self._log(f"{len(uids)} candidate message(s); re-checking headers")

        return self._confirm(uids, criteria, folder)

    # ------------------------------------------------------------------------
    def plan_actions(
        self,
        criteria: Criteria,
        source: str,
        destination: str = "",
        flags: Sequence[str] = (),
        discard: bool = False,
    ) -> MailActionPlan:
        """Work out what the existing-mail pass would do, without doing it.

        Read-only by construction -- the mailbox is opened read-only and
        nothing is written -- so a caller can always build a plan first and
        decide afterwards. That is what a dry run is: a plan that is never
        executed, rather than a flag threaded down into the operations.
        """
        messages = self.search(criteria, source, readonly=True)

        return MailActionPlan(
            source=source,
            destination=destination,
            flags=list(flags),
            discard=discard,
            messages=messages,
        )

    # ------------------------------------------------------------------------
    def execute(self, plan: MailActionPlan) -> MailActionResult:
        """Carry out a plan and report what was done.

        Flags are applied before any move, because a move invalidates the
        UIDs the flag call would otherwise use. Nothing here asks the user
        anything: whether a plan should run at all is the caller's decision,
        already made by the time this is called.
        """
        if plan.is_empty:
            return MailActionResult()

        uids = plan.uids

        self._select(plan.source, readonly=False)

        flagged = 0

        if plan.flags:
            self.add_flags(uids, [flag.encode() for flag in plan.flags])
            flagged = len(uids)

        if plan.discard:
            return MailActionResult(flagged=flagged, deleted=self.delete(uids))

        if plan.moves:
            moved = self.move(uids, plan.destination)

            return MailActionResult(flagged=flagged, moved=moved)

        return MailActionResult(flagged=flagged)

    # ------------------------------------------------------------------------
    def _confirm(
        self, uids: list[int], criteria: Criteria, folder: str
    ) -> list[MessageSummary]:
        """Fetch headers for candidates and keep only the real matches."""
        client = self._require_client()

        try:
            fetched = client.fetch(uids, ["BODY.PEEK[HEADER]", "INTERNALDATE"])

        except IMAPClientError as exc:
            raise MxFilterError(f"IMAP fetch failed -- {exc}") from exc

        matches = []

        for uid, data in sorted(fetched.items()):
            raw = data.get(b"BODY[HEADER]") or b""
            message = email.message_from_bytes(raw)
            headers = header_values(message)

            if not criteria.matches(headers):
                continue

            internal = data.get(b"INTERNALDATE")

            matches.append(
                MessageSummary(
                    uid=uid,
                    date=(
                        internal.strftime("%Y-%m-%d %H:%M:%S")
                        if internal
                        else ""
                    ),
                    sender=decode_header_value(message.get("From", "")),
                    subject=decode_header_value(message.get("Subject", "")),
                    folder=folder,
                )
            )

        return matches

    # ------------------------------------------------------------------------
    def _select(self, folder: str, readonly: bool = True) -> None:
        """Select a folder, naming it in the error if it is missing."""
        client = self._require_client()

        try:
            client.select_folder(folder, readonly=readonly)

        except IMAPClientError as exc:
            raise MxFilterError(
                f"cannot open folder {folder!r} -- {exc}. Run 'mxfilter "
                f"folders' to see the exact names this server uses."
            ) from exc

    # ------------------------------------------------------------------------
    def add_flags(self, uids: list[int], flags: list[str]) -> None:
        """Set flags on messages in the currently selected folder."""
        client = self._require_client()
        self._log(f"flagging {len(uids)} message(s) with {flags}")

        try:
            client.add_flags(uids, flags)

        except IMAPClientError as exc:
            raise MxFilterError(f"could not set flags -- {exc}") from exc

    # ------------------------------------------------------------------------
    def move(self, uids: list[int], destination: str) -> int:
        """Move messages out of the selected folder into ``destination``.

        Prefers RFC 6851 MOVE, which is atomic. The fallback is the classic
        COPY + \\Deleted + EXPUNGE dance; UID EXPUNGE is used when UIDPLUS
        is advertised so that only the copied messages are expunged, never
        someone else's concurrently-deleted mail.
        """
        client = self._require_client()

        if not uids:
            return 0

        try:
            if client.has_capability("MOVE"):
                self._log(f"MOVE {len(uids)} message(s) to {destination!r}")
                client.move(uids, destination)

                return len(uids)

            self._log(
                f"server has no MOVE; COPY+EXPUNGE {len(uids)} message(s) "
                f"to {destination!r}"
            )

            client.copy(uids, destination)
            client.add_flags(uids, [b"\\Deleted"])

            if client.has_capability("UIDPLUS"):
                client.uid_expunge(uids)

            else:
                client.expunge()

        except IMAPClientError as exc:
            raise MxFilterError(
                f"could not move messages to {destination!r} -- {exc}"
            ) from exc

        return len(uids)

    # ------------------------------------------------------------------------
    def delete(self, uids: list[int]) -> int:
        """Delete messages from the selected folder, permanently."""
        client = self._require_client()

        if not uids:
            return 0

        self._log(f"deleting {len(uids)} message(s)")

        try:
            client.add_flags(uids, [b"\\Deleted"])

            if client.has_capability("UIDPLUS"):
                client.uid_expunge(uids)

            else:
                client.expunge()

        except IMAPClientError as exc:
            raise MxFilterError(f"could not delete messages -- {exc}") from exc

        return len(uids)

    # ------------------------------------------------------------------------
    def fetch_message_headers(self, folder: str, uid: int):
        """Return one message's headers, for ``from-message``."""
        client = self._require_client()
        self._select(folder, readonly=True)

        try:
            fetched = client.fetch([uid], ["BODY.PEEK[HEADER]"])

        except IMAPClientError as exc:
            raise MxFilterError(f"IMAP fetch failed -- {exc}") from exc

        data = fetched.get(uid)

        if not data:
            raise MxFilterError(f"no message with uid {uid} in {folder!r}")

        return email.message_from_bytes(data.get(b"BODY[HEADER]") or b"")

    # ------------------------------------------------------------------------
    def raw_search(self, folder: str, expression: str) -> list[int]:
        """Run a raw IMAP SEARCH expression, for ``from-message --search``."""
        client = self._require_client()
        self._select(folder, readonly=True)

        self._log(f"raw search in {folder!r}: {expression}")

        try:
            return list(client.search(expression))

        except IMAPClientError as exc:
            raise MxFilterError(
                f"IMAP search {expression!r} failed -- {exc}. Use IMAP "
                f"syntax, e.g. 'FROM boss@example.com' or 'UNSEEN'."
            ) from exc

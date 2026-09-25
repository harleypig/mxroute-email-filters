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

Every bulk command is split into fixed-size batches of UIDs, because
IMAPClient puts the whole set on one command line and servers limit how
long that line may be. Batching is purely a transport concern and is
independent of ``--max-messages``, which is a policy ceiling the user sets;
tying the two together is what let raising the cap quietly remove a
protection (issue #24). The cost is that a bulk operation is no longer one
command, so it can now stop half way -- ``PartialBatchError`` is what says
so, and says which messages made it.

Existing and visible are also two different questions, which is why both
folder views are cached. ``LIST`` reports what the account has; ``LSUB``
reports what the user subscribed to, and a webmail client -- Roundcube
included -- draws its folder tree from ``LSUB``. A folder that was created
but never subscribed therefore exists, receives mail, and is invisible in
webmail: the same failure as filing into the wrong folder, arrived at from
the other direction.
"""

import contextlib
import email
import socket
import ssl
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from typing import ClassVar, NoReturn

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError, LoginError

from . import MxFilterError
from .config import Config
from .criteria import Criteria

__all__ = [
    "UID_BATCH_SIZE",
    "FolderCreation",
    "ImapSession",
    "MailActionPlan",
    "MailActionResult",
    "MessageSummary",
    "PartialBatchError",
    "decode_header_value",
    "normalize_folder",
    "split_path",
    "uid_batches",
]


# ############################################################################
# Batching
# ############################################################################

# How many UIDs go into a single IMAP command.
#
# IMAPClient joins a UID set with commas and never collapses runs into
# ranges, so the command line grows linearly with the number of messages: a
# UID is at most ten digits plus its comma, which puts 600 of them at about
# 6.6 kB in the worst case and nearer 4 kB with the four- to six-digit UIDs
# a personal mailbox actually has.
#
# The number is a budget, not a measurement. Nothing in IMAP advertises how
# long a command line a server will accept, so there is nothing to discover
# and the only honest option is a conservative constant (CONVENTIONS.md >
# Discover, don't hardcode). The budget itself comes from RFC 2683 s3.2.1.5,
# which asks servers to allow "a command line of at least 8000 octets" -- a
# worst case of 6.6 kB therefore fits inside the shortest line any server is
# expected to take, with room left for the command and the tag.
#
# It is deliberately NOT the --max-messages default, and must never be
# derived from it. --max-messages is a policy ceiling the user sets on how
# much mail to touch; this is a transport detail they should never have to
# think about. Coupling the two is the defect this constant removes (issue
# #24): the cap was silently doing a second, undocumented job, so raising
# it -- the one thing it exists for -- took away a protection nobody knew
# was there.
#
# ICEBOX: collapsing contiguous UIDs into ranges ("1:600") is what RFC 2683
# actually recommends, and it would shrink the command line by an order of
# magnitude, letting each batch carry far more messages. Deliberately not
# done: UID range compression has to be exactly right or it moves mail the
# user never asked about, and a plain count is enough at personal-mailbox
# scale. Revisit only if batching itself becomes a bottleneck.
UID_BATCH_SIZE = 600


# ----------------------------------------------------------------------------
def uid_batches(
    uids: Sequence[int], size: int = UID_BATCH_SIZE
) -> Iterator[list[int]]:
    """Split a UID list into command-sized batches, in order.

    Order is preserved because it is what makes a partial failure
    describable: the messages that got through are a prefix of the list, so
    the report can say "everything up to UID N", rather than handing back a
    scatter the user has to reconcile by hand.
    """
    if size < 1:
        raise MxFilterError(f"batch size must be at least 1, got {size}")

    for start in range(0, len(uids), size):
        yield list(uids[start : start + size])


# ----------------------------------------------------------------------------
def summarize_uids(uids: Sequence[int], shown: int = 10) -> str:
    """Render a UID list compactly enough to put in an error message."""
    if not uids:
        return "none"

    head = ", ".join(str(uid) for uid in uids[:shown])

    if len(uids) <= shown:
        return head

    return f"{head} and {len(uids) - shown} more"


class PartialBatchError(MxFilterError):
    """A bulk operation stopped part way through, having done some of it.

    Batching is what makes this reachable. One IMAP command either happens
    or does not; seven of them can stop at the third, leaving two done, one
    failed, and four never attempted. Reporting that as a plain failure
    would be wrong in the expensive direction -- the user would re-run
    believing nothing had happened, or give up believing everything had.

    So the outcome is carried as data: which UIDs went through, which did
    not, and (for the COPY+EXPUNGE fallback only) which ended up in both
    folders because the copy landed and the removal did not. ``result``
    records what the whole plan achieved, since flagging happens before any
    move and may well have finished.
    """

    # A verb that reads correctly in "N message(s) were ___".
    PAST_TENSE: ClassVar[dict[str, str]] = {
        "flag": "flagged",
        "move": "moved",
        "delete": "deleted",
    }

    # ------------------------------------------------------------------------
    def __init__(
        self,
        operation: str,
        completed: Sequence[int],
        remaining: Sequence[int],
        reason: str,
        destination: str = "",
        source: str = "",
        duplicated: Sequence[int] = (),
    ):
        super().__init__(operation)

        self.operation = operation
        self.completed = tuple(completed)
        self.remaining = tuple(remaining)
        self.duplicated = tuple(duplicated)
        self.reason = reason
        self.destination = destination
        self.source = source
        self.result = MailActionResult()

    # ------------------------------------------------------------------------
    @property
    def total(self) -> int:
        """How many messages the operation set out to handle."""
        return len(self.completed) + len(self.duplicated) + len(self.remaining)

    # ------------------------------------------------------------------------
    def __str__(self) -> str:
        """Say what happened, what did not, and what to do about it.

        Written as one paragraph of plain sentences, the way the other
        actionable failures in this tool are, and deliberately free of the
        word "batch": how the work was split up is a transport detail, and
        a user reading this needs to know what state their mail is in, not
        how the command was framed.
        """
        return " ".join(
            part
            for part in (
                self._what_happened(),
                self._what_is_left(),
                self._what_else_was_done(),
                self._what_to_do_next(),
            )
            if part
        )

    # ------------------------------------------------------------------------
    def _what_happened(self) -> str:
        """The headline: how many of how many, and why it stopped."""
        verb = self.PAST_TENSE.get(self.operation, self.operation)
        where = f" from {self.source!r}" if self.source else ""
        target = f" to {self.destination!r}" if self.destination else ""

        return (
            f"{len(self.completed)} of {self.total} message(s) were {verb}"
            f"{where}{target} and then the server failed -- {self.reason}."
        )

    # ------------------------------------------------------------------------
    def _what_is_left(self) -> str:
        """Which messages were not touched, named precisely."""
        if not self.remaining:
            return ""

        boundary = ""

        if self.completed:
            boundary = (
                f"Every match with a UID at or below {max(self.completed)} "
                f"was handled. "
            )

        return (
            f"{boundary}{len(self.remaining)} message(s) were not touched "
            f"(UIDs {summarize_uids(self.remaining)})."
        )

    # ------------------------------------------------------------------------
    def _what_else_was_done(self) -> str:
        """Steps that ran to completion before this one stopped."""
        if self.operation == "flag" or not self.result.flagged:
            return ""

        return f"Flags were applied to all {self.result.flagged} message(s)."

    # ------------------------------------------------------------------------
    def _what_to_do_next(self) -> str:
        """The recovery, which is different when copies were stranded.

        Re-running is normally safe, and that is worth saying outright: the
        command searches the source folder again, so it finds only what is
        still there. The exception is a copy that could not be removed --
        those messages are in both folders, so a re-run would copy them a
        second time.
        """
        if not self.duplicated:
            return (
                "Re-running the same command is safe: it searches again, so "
                "it acts only on what is left."
            )

        return (
            f"WARNING: {len(self.duplicated)} message(s) were copied to "
            f"{self.destination!r} but could not be removed from "
            f"{self.source or 'the source folder'!r}, so they are now in "
            f"both (UIDs {summarize_uids(self.duplicated)}). Re-running "
            f"would copy them again -- delete them from one side first, "
            f"then re-run to finish the rest."
        )


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


@dataclass(frozen=True)
class FolderCreation:
    """What creating a folder actually achieved.

    Creating and subscribing are two IMAP operations, so they can disagree:
    the folder can exist while the subscription that makes it visible does
    not. Reporting them as one boolean would lose exactly the case this
    record exists for, so the outcome is returned rather than reduced to
    "it worked".

    ``subscribed`` false with an empty ``subscribe_error`` means the caller
    declined to subscribe; with an error it means the attempt failed. The
    folder exists either way -- mail filed there will arrive.
    """

    folder: str
    subscribed: bool
    subscribe_error: str = ""


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
        self._subscribed: list[str] = []

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
            f"{len(self._folders)} folders, "
            f"{len(self._subscribed)} subscribed"
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
        """Cache both folder views, plus the hierarchy delimiter.

        LIST and LSUB answer different questions -- what exists, and what a
        client will draw -- and the second one is not derivable from the
        first. Reading only LIST is what let a created-but-unsubscribed
        folder look present to this tool while being invisible in webmail.

        An LSUB failure is raised rather than shrugged off, because an
        empty subscription list is indistinguishable from "nothing is
        subscribed": the tool would then report the very condition it is
        supposed to detect, confidently and wrongly.
        """
        client = self._require_client()

        try:
            listing = client.list_folders()

        except IMAPClientError as exc:
            raise MxFilterError(f"LIST failed -- {exc}") from exc

        try:
            subscribed = client.list_sub_folders()

        except IMAPClientError as exc:
            raise MxFilterError(f"LSUB failed -- {exc}") from exc

        self._folders, delimiter = self._decode_listing(listing)
        self._subscribed, _ = self._decode_listing(subscribed)

        if delimiter:
            self._delimiter = delimiter

    # ------------------------------------------------------------------------
    @staticmethod
    def _decode_listing(listing) -> tuple[list[str], str]:
        """Turn a LIST/LSUB response into names and the delimiter it used."""
        folders = []
        delimiter = ""

        for _flags, separator, name in listing:
            if separator:
                delimiter = (
                    separator.decode()
                    if isinstance(separator, bytes)
                    else str(separator)
                )

            folders.append(
                name.decode() if isinstance(name, bytes) else str(name)
            )

        return folders, delimiter

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
    @property
    def subscribed_folders(self) -> list[str]:
        """Every folder the account is subscribed to (LSUB)."""
        return list(self._subscribed)

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
    def is_subscribed(self, folder: str) -> bool:
        """Whether a (already normalized) folder is subscribed to.

        Answered from LSUB, never from LIST: existing is what LIST reports,
        and being drawn in a mail client is what this reports.
        """
        return any(
            candidate.casefold() == folder.casefold()
            for candidate in self._subscribed
        )

    # ------------------------------------------------------------------------
    def subscribe(self, folder: str) -> None:
        """Subscribe to a folder, and confirm the server agrees it took.

        The confirmation is not ceremony. No server advertises whether its
        CREATE subscribes on its own, and a SUBSCRIBE that returns OK is
        still only the server's word for it -- so the only way to know is
        to re-read LSUB and look. Assuming the write took is precisely the
        mistake that shipped the invisible folder.
        """
        client = self._require_client()
        self._log(f"subscribing to folder {folder!r}")

        try:
            client.subscribe_folder(folder)

        except IMAPClientError as exc:
            raise MxFilterError(
                f"could not subscribe to folder {folder!r} -- {exc}"
            ) from exc

        self._read_folders()

        if not self.is_subscribed(folder):
            raise MxFilterError(
                f"the server accepted SUBSCRIBE for {folder!r} but still "
                f"does not list it as subscribed (LSUB), so mail clients "
                f"will not show it"
            )

    # ------------------------------------------------------------------------
    def unsubscribe(self, folder: str) -> None:
        """Unsubscribe from a folder, hiding it from mail clients."""
        client = self._require_client()
        self._log(f"unsubscribing from folder {folder!r}")

        try:
            client.unsubscribe_folder(folder)

        except IMAPClientError as exc:
            raise MxFilterError(
                f"could not unsubscribe from folder {folder!r} -- {exc}"
            ) from exc

        self._read_folders()

    # ------------------------------------------------------------------------
    def create_folder(
        self, folder: str, subscribe: bool = True
    ) -> FolderCreation:
        """Create a folder, subscribe to it, and report what happened.

        Subscribing is the default because a folder made to be a
        ``fileinto`` target is by definition one the user is meant to see;
        an unsubscribed one receives mail that never appears in webmail.

        A failed subscription does **not** undo the creation and does not
        raise: the folder exists and mail filed there will arrive, so
        tearing it back down would trade a visibility problem for a data
        one. The outcome is returned instead, for the caller to say out
        loud.
        """
        client = self._require_client()
        self._log(f"creating folder {folder!r}")

        try:
            client.create_folder(folder)

        except IMAPClientError as exc:
            raise MxFilterError(
                f"could not create folder {folder!r} -- {exc}"
            ) from exc

        self._read_folders()

        if not subscribe:
            return FolderCreation(folder=folder, subscribed=False)

        try:
            self.subscribe(folder)

        except MxFilterError as exc:
            return FolderCreation(
                folder=folder, subscribed=False, subscribe_error=str(exc)
            )

        return FolderCreation(folder=folder, subscribed=True)

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
            with self._attributed_to(plan, "flagged"):
                self.add_flags(uids, [flag.encode() for flag in plan.flags])

            flagged = len(uids)

        if plan.discard:
            with self._attributed_to(plan, "deleted", flagged=flagged):
                deleted = self.delete(uids)

            return MailActionResult(flagged=flagged, deleted=deleted)

        if plan.moves:
            with self._attributed_to(plan, "moved", flagged=flagged):
                moved = self.move(uids, plan.destination)

            return MailActionResult(flagged=flagged, moved=moved)

        return MailActionResult(flagged=flagged)

    # ------------------------------------------------------------------------
    @contextlib.contextmanager
    def _attributed_to(
        self, plan: MailActionPlan, counted: str, flagged: int = 0
    ):
        """Attach plan context to a partial failure on its way out.

        The bulk operations know how far they got; only the plan knows
        which folder the mail came from and what the earlier steps
        achieved. Joining the two here completes the report without giving
        the low-level operations a view of the plan they have no other use
        for.

        ``counted`` names the ``MailActionResult`` field the completed
        count belongs in, and ``flagged`` carries forward a flagging step
        that already finished.
        """
        try:
            yield

        except PartialBatchError as exc:
            exc.source = plan.source
            exc.result = MailActionResult(
                **{"flagged": flagged, counted: len(exc.completed)}
            )

            raise

    # ------------------------------------------------------------------------
    def _confirm(
        self, uids: list[int], criteria: Criteria, folder: str
    ) -> list[MessageSummary]:
        """Fetch headers for candidates and keep only the real matches.

        Batched like every other bulk command, and for the same reason: the
        candidate set is whatever the server's SEARCH returned, which no cap
        in this tool bounds. A folder with thousands of matches would
        otherwise put thousands of UIDs on one FETCH line.
        """
        client = self._require_client()
        fetched: dict = {}

        for batch in uid_batches(uids):
            try:
                fetched.update(
                    client.fetch(batch, ["BODY.PEEK[HEADER]", "INTERNALDATE"])
                )

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
        self._log(
            f"flagging {len(uids)} message(s) with {flags} "
            f"in batches of up to {UID_BATCH_SIZE}"
        )

        done: list[int] = []

        for batch in uid_batches(uids):
            try:
                client.add_flags(batch, flags)

            except IMAPClientError as exc:
                if not done:
                    raise MxFilterError(
                        f"could not set flags -- {exc}"
                    ) from exc

                raise PartialBatchError(
                    operation="flag",
                    completed=done,
                    remaining=uids[len(done) :],
                    reason=str(exc),
                ) from exc

            done.extend(batch)

    # ------------------------------------------------------------------------
    def move(self, uids: list[int], destination: str) -> int:
        """Move messages out of the selected folder into ``destination``.

        Prefers RFC 6851 MOVE, which is atomic per command. The fallback is
        the classic COPY + \\Deleted + EXPUNGE dance; UID EXPUNGE is used
        when UIDPLUS is advertised so that only the copied messages are
        expunged, never someone else's concurrently-deleted mail.
        """
        client = self._require_client()

        if not uids:
            return 0

        if client.has_capability("MOVE"):
            return self._move_natively(client, uids, destination)

        return self._move_by_copying(client, uids, destination)

    # ------------------------------------------------------------------------
    def _move_natively(
        self, client: IMAPClient, uids: list[int], destination: str
    ) -> int:
        """MOVE in batches; a failed one leaves its mail where it was."""
        self._log(
            f"MOVE {len(uids)} message(s) to {destination!r} in batches of "
            f"up to {UID_BATCH_SIZE}"
        )

        done: list[int] = []

        for batch in uid_batches(uids):
            try:
                client.move(batch, destination)

            except IMAPClientError as exc:
                self._move_failed(uids, done, destination, exc)

            done.extend(batch)

        return len(done)

    # ------------------------------------------------------------------------
    def _move_by_copying(
        self, client: IMAPClient, uids: list[int], destination: str
    ) -> int:
        """COPY + \\Deleted + EXPUNGE, for a server with no MOVE.

        Three commands per batch instead of one, which is what makes the
        stranded-copy case reachable: a copy that lands and a removal that
        does not leaves the same mail in both folders. So the expunge is
        issued even on the way out, finishing every batch that got as far
        as being marked -- that is what keeps "re-running is safe" true for
        the part that did complete.
        """
        self._log(
            f"server has no MOVE; COPY+EXPUNGE {len(uids)} message(s) to "
            f"{destination!r} in batches of up to {UID_BATCH_SIZE}"
        )

        copied: list[int] = []
        stranded: list[int] = []
        failure: IMAPClientError | None = None

        for batch in uid_batches(uids):
            try:
                client.copy(batch, destination)

            except IMAPClientError as exc:
                failure = exc
                break

            try:
                client.add_flags(batch, [b"\\Deleted"])

            except IMAPClientError as exc:
                stranded = batch
                failure = exc
                break

            copied.extend(batch)

        try:
            self._expunge(client, copied)

        except IMAPClientError as exc:
            # The removal itself failed, so nothing was really moved: every
            # copied message is now in both folders.
            self._move_failed(
                uids, [], destination, exc, duplicated=[*copied, *stranded]
            )

        if failure is not None:
            self._move_failed(uids, copied, destination, failure, stranded)

        return len(copied)

    # ------------------------------------------------------------------------
    @staticmethod
    def _move_failed(
        uids: list[int],
        completed: list[int],
        destination: str,
        exc: IMAPClientError,
        duplicated: Sequence[int] = (),
    ) -> NoReturn:
        """Raise the right failure for a move that did not finish.

        A move that achieved nothing is an ordinary failure and keeps the
        wording it always had. One that achieved something is a different
        event and says so, because the difference decides what the user
        does next.
        """
        if not completed and not duplicated:
            raise MxFilterError(
                f"could not move messages to {destination!r} -- {exc}"
            ) from exc

        offset = len(completed) + len(duplicated)

        raise PartialBatchError(
            operation="move",
            completed=completed,
            remaining=uids[offset:],
            reason=str(exc),
            destination=destination,
            duplicated=duplicated,
        ) from exc

    # ------------------------------------------------------------------------
    @staticmethod
    def _expunge(client: IMAPClient, uids: list[int]) -> None:
        """Drop the \\Deleted messages, by UID where the server allows it.

        With UIDPLUS the expunge is batched like everything else. Without
        it there is no UID argument to batch -- EXPUNGE takes none -- so it
        is issued once at the end rather than once per batch. That also
        keeps the number of times a concurrent client's deleted mail can be
        swept up at exactly one, as it was before batching.
        """
        if not uids:
            return

        if client.has_capability("UIDPLUS"):
            for batch in uid_batches(uids):
                client.uid_expunge(batch)

            return

        client.expunge()

    # ------------------------------------------------------------------------
    def delete(self, uids: list[int]) -> int:
        """Delete messages from the selected folder, permanently."""
        client = self._require_client()

        if not uids:
            return 0

        self._log(
            f"deleting {len(uids)} message(s) in batches of up to "
            f"{UID_BATCH_SIZE}"
        )

        marked: list[int] = []
        failure: IMAPClientError | None = None

        for batch in uid_batches(uids):
            try:
                client.add_flags(batch, [b"\\Deleted"])

            except IMAPClientError as exc:
                failure = exc
                break

            marked.extend(batch)

        try:
            self._expunge(client, marked)

        except IMAPClientError as exc:
            # Marking succeeded and the removal did not, so nothing is
            # actually gone -- report it as having deleted none.
            failure = exc
            marked = []

        if failure is None:
            return len(marked)

        if not marked:
            raise MxFilterError(
                f"could not delete messages -- {failure}"
            ) from failure

        raise PartialBatchError(
            operation="delete",
            completed=marked,
            remaining=uids[len(marked) :],
            reason=str(failure),
        ) from failure

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

"""Shared fixtures for the offline tier.

Everything here is offline by construction. The IMAP double below stands in
for ``IMAPClient`` at the module boundary -- it is never a stand-in for
anything mxfilter owns, so a test that passes against it is still testing
mxfilter's logic rather than its own mock.

Two hazards this file exists to remove:

* The real user's ``config.toml`` and ``MXROUTE_*`` variables would
  otherwise leak into ``load_config`` and make the resolution-order tests
  depend on the machine they run on.
* A test that forgot to patch ``IMAPClient`` would open a socket. The
  ``no_network`` fixture below turns that into an immediate named failure
  rather than a slow, flaky pass against a real host.
"""

import datetime
import os
import socket

import pytest
from sievelib import parser

from mxfilter import imap as imap_module
from mxfilter.config import Config, Secret

# ############################################################################
# Environment isolation
# ############################################################################


# ----------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    """Detach every test from the developer's own account settings.

    ``load_config`` reads ``$XDG_CONFIG_HOME/mxfilter/config.toml`` and the
    ``MXROUTE_*`` variables. Without this the suite would pass or fail
    depending on whose shell it ran in, and a real password could reach a
    test's assertion output.
    """
    for name in list(os.environ):
        if name.startswith("MXROUTE_"):
            monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


# ----------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Make an accidental connection fail loudly instead of succeeding.

    The offline tier's defining property is that it opens no socket. A
    test that forgot to patch ``IMAPClient`` or ``managesieve.Client``
    would otherwise reach a real host -- slowly, flakily, and with the
    developer's own credentials -- and still pass. This turns that into an
    immediate, named failure.

    ``socket.socket`` is the one chokepoint both client libraries go
    through, so patching it needs no knowledge of either.
    """

    def refuse(*args, **kwargs):
        raise RuntimeError(
            "this test tried to open a network connection; the offline "
            "tier must patch the client at mxfilter's import boundary "
            "(see the fake_imap fixture)"
        )

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# ############################################################################
# Sieve fixtures
# ############################################################################

# A script in the shape Roundcube's managesieve plugin writes: tab-indented,
# brace on its own line, and a `# rule:[name]` marker rather than sievelib's
# own `# Filter:` comment. This is the script the merge must not destroy --
# it is what an MXroute account actually has in it before mxfilter ever
# runs (ADR 0002).
ROUNDCUBE_SCRIPT = """require ["fileinto","imap4flags"];
# rule:[keep-boss]
if header :contains "from" "boss@example.com"
{
\tfileinto "INBOX.Boss";
\tsetflag "\\\\Flagged";
\tstop;
}
# rule:[bin-the-noise]
if header :contains "subject" "newsletter"
{
\tfileinto "INBOX.Noise";
\tstop;
}
"""


# ----------------------------------------------------------------------------
@pytest.fixture
def roundcube_script() -> str:
    """The hand-made script a merge has to carry through untouched."""
    return ROUNDCUBE_SCRIPT


# ----------------------------------------------------------------------------
def _reparse(text: str) -> parser.Parser:
    """Parse ``text`` with sievelib, failing the test if it will not.

    Round-tripping through sievelib's own parser is the only check that
    actually proves the emitted script is loadable; string assertions alone
    would happily pass on syntactically broken output.
    """
    script_parser = parser.Parser()

    assert script_parser.parse(text), (
        f"emitted script does not reparse: "
        f"{getattr(script_parser, 'error', 'unknown parse error')}\n{text}"
    )

    return script_parser


# ----------------------------------------------------------------------------
@pytest.fixture
def reparse():
    """Hand tests the sievelib round-trip check as a callable."""
    return _reparse


# ############################################################################
# IMAP double
# ############################################################################


class FakeIMAPClient:
    """A stand-in for ``IMAPClient``, recording what it was asked to do.

    Only the handful of methods ``ImapSession`` calls are implemented. It
    deliberately does not emulate IMAP semantics -- the point is to observe
    the calls mxfilter makes and to feed it canned responses, not to
    reimplement a server.
    """

    # ------------------------------------------------------------------------
    def __init__(self):
        """Start out as an empty INBOX on a Maildir++ style server."""
        self.calls: list[tuple] = []
        self.connected_to: tuple | None = None
        self.starttls_called = False
        self.listing = [
            ((), b".", b"INBOX"),
            ((), b".", b"INBOX.Lists"),
            ((), b".", b"INBOX.spam"),
        ]
        self.messages: dict[int, bytes] = {}
        self.caps = {"MOVE", "UIDPLUS"}
        self.failures: dict[str, Exception] = {}

    # ------------------------------------------------------------------------
    def _maybe_fail(self, name: str) -> None:
        """Raise whatever the test armed this method with."""
        error = self.failures.get(name)

        if error is not None:
            raise error

    # ------------------------------------------------------------------------
    def login(self, user, password) -> None:
        self._maybe_fail("login")
        self.calls.append(("login", user))

    # ------------------------------------------------------------------------
    def starttls(self) -> None:
        self.starttls_called = True

    # ------------------------------------------------------------------------
    def logout(self) -> None:
        self.calls.append(("logout",))

    # ------------------------------------------------------------------------
    def list_folders(self):
        self._maybe_fail("list_folders")

        return self.listing

    # ------------------------------------------------------------------------
    def capabilities(self):
        return [name.encode() for name in sorted(self.caps)]

    # ------------------------------------------------------------------------
    def has_capability(self, name: str) -> bool:
        return name in self.caps

    # ------------------------------------------------------------------------
    def create_folder(self, folder: str) -> None:
        self._maybe_fail("create_folder")
        self.calls.append(("create_folder", folder))
        self.listing.append(((), b".", folder.encode()))

    # ------------------------------------------------------------------------
    def select_folder(self, folder: str, readonly: bool = True) -> None:
        self._maybe_fail("select_folder")
        self.calls.append(("select_folder", folder, readonly))

    # ------------------------------------------------------------------------
    def search(self, key):
        self._maybe_fail("search")
        self.calls.append(("search", key))

        return sorted(self.messages)

    # ------------------------------------------------------------------------
    def fetch(self, uids, parts):
        self._maybe_fail("fetch")
        self.calls.append(("fetch", tuple(uids)))

        stamp = datetime.datetime(2026, 2, 3, 4, 5, 6)

        return {
            uid: {
                b"BODY[HEADER]": self.messages[uid],
                b"INTERNALDATE": stamp,
            }
            for uid in uids
            if uid in self.messages
        }

    # ------------------------------------------------------------------------
    def add_flags(self, uids, flags) -> None:
        self._maybe_fail("add_flags")
        self.calls.append(("add_flags", tuple(uids), tuple(flags)))

    # ------------------------------------------------------------------------
    def move(self, uids, destination) -> None:
        self._maybe_fail("move")
        self.calls.append(("move", tuple(uids), destination))

    # ------------------------------------------------------------------------
    def copy(self, uids, destination) -> None:
        self._maybe_fail("copy")
        self.calls.append(("copy", tuple(uids), destination))

    # ------------------------------------------------------------------------
    def uid_expunge(self, uids) -> None:
        self.calls.append(("uid_expunge", tuple(uids)))

    # ------------------------------------------------------------------------
    def expunge(self) -> None:
        self.calls.append(("expunge",))

    # ------------------------------------------------------------------------
    def names(self) -> list[str]:
        """Return just the recorded method names, for order assertions."""
        return [call[0] for call in self.calls]


# ----------------------------------------------------------------------------
@pytest.fixture
def fake_imap(monkeypatch) -> FakeIMAPClient:
    """Patch ``IMAPClient`` at mxfilter's boundary and hand back the double."""
    client = FakeIMAPClient()

    def factory(host, port=None, ssl=True):
        client.connected_to = (host, port, ssl)

        return client

    monkeypatch.setattr(imap_module, "IMAPClient", factory)

    return client


# ----------------------------------------------------------------------------
@pytest.fixture
def imap_config() -> Config:
    """A Config with the credential already resolved, so nothing prompts."""
    config = Config(
        host="mail.example.com",
        imap_host="mail.example.com",
        user="user@example.com",
    )
    config._password = Secret("not-a-real-password")

    return config


# ----------------------------------------------------------------------------
@pytest.fixture
def imap_session(fake_imap, imap_config):
    """An opened ImapSession wired to the double."""
    session = imap_module.ImapSession(imap_config)
    session.open()

    return session

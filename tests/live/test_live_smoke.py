"""Read-only smoke tests against a real MXroute account.

This tier is also the repo's end-to-end pass (TESTS.md): a CLI that talks
to two servers has no meaningful integration layer between "offline logic"
and "does it work against MXroute".

Everything here is **read-only**. It exercises exactly the claims the
offline tier cannot make -- that the ManageSieve port and TLS mode are
right, and that the folder delimiter this account reports is the one the
offline normalization was fed. Both are recorded as unconfirmed or merely
likely in CONVENTIONS.md > Confidence, which is why reading them off the
server is the whole point.

Nothing here writes. Adding a test that does means adding the backup and
restore fixture the safety contract in ``conftest.py`` describes first.
"""

import pytest

from mxfilter.imap import ImapSession
from mxfilter.sieve import SieveSession

pytestmark = pytest.mark.live


# ############################################################################
# ManageSieve
# ############################################################################


# ----------------------------------------------------------------------------
def test_the_sieve_port_and_tls_mode_actually_connect(live_config):
    """4190 + STARTTLS is an RFC default, not a documented MXroute fact.

    If this fails, the answer is a ``--sieve-port`` / ``--sieve-tls``
    combination that works, recorded in CONVENTIONS.md -- not a change to
    the tool's defaults.
    """
    with SieveSession(live_config) as session:
        capabilities = session.capabilities()

    assert capabilities, "server advertised no Sieve extensions at all"


# ----------------------------------------------------------------------------
def test_the_active_script_can_be_read_back(live_config):
    """The merge depends on downloading and parsing whatever is there.

    An account with no active script yet is a valid state, so only the
    download is asserted -- not that a script exists.
    """
    with SieveSession(live_config) as session:
        active = session.active_script_name()

        if active is None:
            pytest.skip("this account has no active Sieve script yet")

        content = session.get_script(active)

    assert isinstance(content, str)


# ############################################################################
# IMAP
# ############################################################################


# ----------------------------------------------------------------------------
def test_the_folder_delimiter_is_discovered_not_assumed(live_config):
    """A ``.`` delimiter is likely on MXroute; likely is not confirmed.

    Guessing it wrong does not raise -- it files mail into a folder nobody
    opens -- so this is the assertion the offline normalization tests
    cannot make for themselves.
    """
    with ImapSession(live_config) as session:
        delimiter = session.delimiter
        folders = session.folders

    assert delimiter, "server reported no hierarchy delimiter"
    assert any(name.upper() == "INBOX" for name in folders)

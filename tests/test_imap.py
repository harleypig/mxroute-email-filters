"""Folder naming, the post-filtered search, and the plan/execute split.

Nothing here opens a socket: ``IMAPClient`` is replaced at mxfilter's own
import boundary by the ``fake_imap`` double, so what is under test is
mxfilter's logic and never IMAPClient's.

Folder naming carries the most risk of anything offline in this tool.
Getting the delimiter wrong does not fail -- it files mail into a folder
nobody opens, which looks exactly like the filter not running.
"""

import pytest
from imapclient.exceptions import IMAPClientError, LoginError

from mxfilter import MxFilterError
from mxfilter.criteria import Criteria
from mxfilter.imap import (
    ImapSession,
    MailActionPlan,
    MessageSummary,
    decode_header_value,
    header_values,
    normalize_folder,
    split_path,
)

# ############################################################################
# Helpers
# ############################################################################


# ----------------------------------------------------------------------------
def message(sender: str, subject: str = "Subject line") -> bytes:
    """Return a minimal RFC 822 header block for the fetch double."""
    return (
        f"From: {sender}\r\nSubject: {subject}\r\nTo: me@example.com\r\n\r\n"
    ).encode()


# ----------------------------------------------------------------------------
def summary(uid: int) -> MessageSummary:
    """Return a MessageSummary, for plan tests that need no real mail."""
    return MessageSummary(
        uid=uid, date="", sender="a@example.com", subject="s", folder="INBOX"
    )


# ############################################################################
# Folder naming
# ############################################################################


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "delimiter", "known", "expected"),
    [
        pytest.param(
            "Lists/GitHub",
            ".",
            None,
            "INBOX.Lists.GitHub",
            id="maildir-new-folder-gains-the-inbox-prefix",
        ),
        pytest.param(
            "INBOX.Lists.GitHub",
            ".",
            None,
            "INBOX.Lists.GitHub",
            id="maildir-name-already-carrying-the-prefix",
        ),
        pytest.param(
            "Lists/GitHub",
            "/",
            None,
            "Lists/GitHub",
            id="slash-server-keeps-it-top-level",
        ),
        pytest.param(
            "INBOX/Lists/GitHub",
            "/",
            None,
            "INBOX/Lists/GitHub",
            id="slash-name-already-carrying-the-prefix",
        ),
        # A dot is only a separator on a server that reports it as one, so
        # on a /-delimited server this stays one literal folder name. That
        # is deliberate: the alternative is deciding that no folder may
        # ever contain a dot in its name.
        pytest.param(
            "INBOX.Lists.GitHub",
            "/",
            None,
            "INBOX.Lists.GitHub",
            id="dots-are-not-separators-on-a-slash-server",
        ),
        pytest.param(
            "INBOX", ".", None, "INBOX", id="inbox-itself-is-untouched"
        ),
        pytest.param(
            "Lists/GitHub",
            ".",
            ["INBOX.Lists.GitHub"],
            "INBOX.Lists.GitHub",
            id="existing-folder-looked-up",
        ),
        pytest.param(
            "inbox.lists.github",
            ".",
            ["INBOX.Lists.GitHub"],
            "INBOX.Lists.GitHub",
            id="lookup-fixes-the-case",
        ),
        pytest.param(
            "spam",
            ".",
            ["INBOX.spam"],
            "INBOX.spam",
            id="lowercase-spam-folder-is-found-not-guessed",
        ),
    ],
)
def test_normalize_folder(name, delimiter, known, expected):
    """``Lists/GitHub`` and ``INBOX.Lists.GitHub`` name the same folder.

    Users type whichever spelling they have seen. The delimiter comes from
    the server's own LIST response, so this function is where a
    ``.``-delimited Maildir++ account and a ``/``-delimited one stop being
    two different user experiences.
    """
    assert normalize_folder(name, delimiter, known) == expected


# ----------------------------------------------------------------------------
def test_normalize_prefers_an_existing_folder_over_the_guess():
    """Matching the server's list is what avoids a near-duplicate folder.

    Filing into ``INBOX.Junk`` when the account's spam folder is
    ``INBOX.spam`` creates a second folder beside the real one, and the
    mail lands where nothing looks.
    """
    assert normalize_folder("spam", ".", ["INBOX.spam"]) == "INBOX.spam"

    # No such folder: the guess is used, and it is a *new* folder name.
    assert normalize_folder("Junk", ".", ["INBOX.spam"]) == "INBOX.Junk"


# ----------------------------------------------------------------------------
def test_normalize_refuses_an_empty_folder_name():
    with pytest.raises(MxFilterError, match="empty folder name"):
        normalize_folder("///", ".")


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "delimiter", "parts"),
    [
        pytest.param("Lists/GitHub", ".", ["Lists", "GitHub"], id="slashes"),
        pytest.param("A.B", ".", ["A", "B"], id="delimiter"),
        pytest.param("A.B/C", ".", ["A", "B", "C"], id="both-mixed"),
        pytest.param("//a//", ".", ["a"], id="empty-components-dropped"),
        pytest.param("A.B", "/", ["A.B"], id="dot-is-not-special-here"),
    ],
)
def test_split_path(name, delimiter, parts):
    assert split_path(name, delimiter) == parts


# ############################################################################
# Header decoding
# ############################################################################


# ----------------------------------------------------------------------------
def test_decode_header_value_decodes_an_encoded_word():
    """Sieve compares against the decoded value, so mxfilter must too."""
    assert decode_header_value("=?utf-8?q?caf=C3=A9?=") == "café"


# ----------------------------------------------------------------------------
def test_decode_header_value_falls_back_to_the_raw_text():
    """A malformed encoded word must not abort the whole retroactive pass."""
    malformed = "=?bogus-charset?q?x?="

    assert decode_header_value(malformed) == malformed


# ----------------------------------------------------------------------------
def test_header_values_keeps_both_the_decoded_and_the_raw_form():
    """Broader candidates cost nothing; a missed match costs a lost mail.

    A user who copied the literal encoded text out of a header dump still
    finds their message, while ``matches`` against the decoded value works
    the way Sieve will.
    """
    import email

    raw = b"Subject: =?utf-8?q?caf=C3=A9?=\r\nFrom: a@example.com\r\n\r\n"
    values = header_values(email.message_from_bytes(raw))

    assert "café" in values["SUBJECT"]
    assert "=?utf-8?q?caf=C3=A9?=" in values["SUBJECT"]
    assert values["FROM"] == ["a@example.com"]


# ############################################################################
# Session lifecycle
# ############################################################################


# ----------------------------------------------------------------------------
def test_open_reads_the_delimiter_and_the_folder_list(imap_session):
    """Discovered, never hardcoded -- MXroute documents neither."""
    assert imap_session.delimiter == "."
    assert imap_session.folders == ["INBOX", "INBOX.Lists", "INBOX.spam"]


# ----------------------------------------------------------------------------
def test_port_993_uses_implicit_tls_and_143_uses_starttls(
    fake_imap, imap_config
):
    """993 and 143 are different protocols, not different port numbers."""
    ImapSession(imap_config).open()

    assert fake_imap.connected_to == ("mail.example.com", 993, True)
    assert fake_imap.starttls_called is False

    imap_config.imap_port = 143
    ImapSession(imap_config).open()

    assert fake_imap.connected_to == ("mail.example.com", 143, False)
    assert fake_imap.starttls_called is True


# ----------------------------------------------------------------------------
def test_a_missing_setting_is_named_before_anything_connects(fake_imap):
    """Failing on the settings is friendlier than failing on the socket."""
    from mxfilter.config import Config, Secret

    config = Config(user="user@example.com")
    config._password = Secret("x")

    with pytest.raises(MxFilterError, match="imap_host"):
        ImapSession(config).open()

    assert fake_imap.connected_to is None


# ----------------------------------------------------------------------------
def test_a_login_failure_names_the_full_address_convention(
    fake_imap, imap_config
):
    """MXroute wants the whole email address; the wrong guess looks generic.

    The message also has to report the credential *state* and never the
    credential.
    """
    fake_imap.failures["login"] = LoginError("no")

    with pytest.raises(MxFilterError, match="FULL email address") as caught:
        ImapSession(imap_config).open()

    assert "not-a-real-password" not in str(caught.value)


# ----------------------------------------------------------------------------
def test_calling_a_method_before_open_fails_clearly(imap_config):
    session = ImapSession(imap_config)

    with pytest.raises(MxFilterError, match="IMAP session is not open"):
        session.search(Criteria(), "INBOX")


# ----------------------------------------------------------------------------
def test_close_is_safe_to_call_twice(imap_session, fake_imap):
    """Cleanup runs on the error path too, where the socket may be gone."""
    imap_session.close()
    imap_session.close()

    assert fake_imap.names().count("logout") == 1


# ----------------------------------------------------------------------------
def test_the_session_works_as_a_context_manager(fake_imap, imap_config):
    with ImapSession(imap_config) as session:
        assert session.delimiter == "."

    assert "logout" in fake_imap.names()


# ----------------------------------------------------------------------------
def test_the_progress_callback_receives_steps_instead_of_printing(
    fake_imap, imap_config
):
    """The core returns data and reports through a callback; only the CLI
    prints (CONVENTIONS.md). A second front-end depends on that holding."""
    seen = []

    ImapSession(imap_config, progress=seen.append).open()

    assert any("connecting to" in line for line in seen)
    assert any("delimiter" in line for line in seen)


# ############################################################################
# Search and the post-filter
# ############################################################################


# ----------------------------------------------------------------------------
def test_search_rejects_an_imap_hit_that_fails_the_strict_comparison(
    imap_session, fake_imap
):
    """The gap between IMAP SEARCH and Sieve, closed in Python.

    IMAP can only substring-match, so searching for the longest literal of
    ``*@lists.example.com`` returns both messages below. Only the bare
    address satisfies Sieve's whole-value ``:matches``; the one with a
    display name must be dropped, or the retroactive pass moves mail the
    server-side filter will leave alone.
    """
    fake_imap.messages = {
        1: message("Announce <announce@lists.example.com>"),
        2: message("announce@lists.example.com"),
        3: message("someone@other.example.com"),
    }

    criteria = Criteria(compare="matches")
    criteria.add("from", "*@lists.example.com")

    found = imap_session.search(criteria, "INBOX")

    assert [item.uid for item in found] == [2]

    # The IMAP side really was broader -- all three were fetched.
    assert ("fetch", (1, 2, 3)) in fake_imap.calls


# ----------------------------------------------------------------------------
def test_search_rejects_a_substring_hit_under_compare_is(
    imap_session, fake_imap
):
    """``--compare is`` is exact; IMAP's SUBJECT key is not."""
    fake_imap.messages = {
        1: message("a@example.com", subject="Re: Weekly Report"),
        2: message("a@example.com", subject="Weekly Report"),
    }

    criteria = Criteria(compare="is")
    criteria.add("subject", "Weekly Report")

    assert [item.uid for item in imap_session.search(criteria, "INBOX")] == [2]


# ----------------------------------------------------------------------------
def test_search_returns_nothing_without_fetching_when_imap_found_nothing(
    imap_session, fake_imap
):
    criteria = Criteria()
    criteria.add("from", "nobody@example.com")

    assert imap_session.search(criteria, "INBOX") == []
    assert "fetch" not in fake_imap.names()


# ----------------------------------------------------------------------------
def test_a_matched_message_carries_its_decoded_summary(
    imap_session, fake_imap
):
    fake_imap.messages = {
        7: b"From: =?utf-8?q?Jos=C3=A9?= <j@example.com>\r\n"
        b"Subject: Hola\r\n\r\n"
    }

    criteria = Criteria()
    criteria.add("from", "j@example.com")

    found = imap_session.search(criteria, "INBOX")

    assert found[0].sender == "José <j@example.com>"
    assert found[0].subject == "Hola"
    assert found[0].date == "2026-02-03 04:05:06"
    assert found[0].folder == "INBOX"


# ----------------------------------------------------------------------------
def test_a_search_failure_names_the_folder(imap_session, fake_imap):
    fake_imap.messages = {1: message("a@example.com")}
    fake_imap.failures["search"] = IMAPClientError("SEARCH rejected")

    criteria = Criteria()
    criteria.add("from", "a@example.com")

    with pytest.raises(MxFilterError, match=r"search in 'INBOX' failed"):
        imap_session.search(criteria, "INBOX")


# ----------------------------------------------------------------------------
def test_selecting_a_missing_folder_points_at_the_folders_command(
    imap_session, fake_imap
):
    """Folder names are discovered, so the error has to say where to look."""
    fake_imap.failures["select_folder"] = IMAPClientError("no such mailbox")

    criteria = Criteria()
    criteria.add("from", "a@example.com")

    with pytest.raises(MxFilterError, match=r"Run 'mxfilter folders'"):
        imap_session.search(criteria, "INBOX.Nope")


# ############################################################################
# Plan / execute
# ############################################################################


# ----------------------------------------------------------------------------
def test_planning_never_writes_anything(imap_session, fake_imap):
    """A dry run is a plan that is not executed, not a flag threaded down.

    The mailbox is opened read-only while planning, so building a plan is
    safe even when the caller then decides against it.
    """
    fake_imap.messages = {1: message("a@example.com")}

    criteria = Criteria()
    criteria.add("from", "a@example.com")

    plan = imap_session.plan_actions(
        criteria, "INBOX", destination="INBOX.Lists", flags=["\\Seen"]
    )

    assert plan.count == 1
    assert ("select_folder", "INBOX", True) in fake_imap.calls

    mutations = {"add_flags", "move", "copy", "expunge", "uid_expunge"}
    assert mutations.isdisjoint(fake_imap.names())


# ----------------------------------------------------------------------------
def test_execute_flags_before_it_moves(imap_session, fake_imap):
    """A move invalidates the UIDs, so the order is load-bearing."""
    fake_imap.messages = {1: message("a@example.com")}

    criteria = Criteria()
    criteria.add("from", "a@example.com")

    plan = imap_session.plan_actions(
        criteria, "INBOX", destination="INBOX.Lists", flags=["\\Seen"]
    )
    result = imap_session.execute(plan)

    names = fake_imap.names()

    assert names.index("add_flags") < names.index("move")
    assert ("add_flags", (1,), (b"\\Seen",)) in fake_imap.calls
    assert ("move", (1,), "INBOX.Lists") in fake_imap.calls
    assert result.flagged == 1
    assert result.moved == 1
    assert result.deleted == 0


# ----------------------------------------------------------------------------
def test_execute_reopens_the_folder_writable(imap_session, fake_imap):
    fake_imap.messages = {1: message("a@example.com")}

    criteria = Criteria()
    criteria.add("from", "a@example.com")

    imap_session.execute(imap_session.plan_actions(criteria, "INBOX"))

    assert ("select_folder", "INBOX", False) in fake_imap.calls


# ----------------------------------------------------------------------------
def test_execute_on_an_empty_plan_touches_nothing(imap_session, fake_imap):
    plan = MailActionPlan(
        source="INBOX",
        destination="INBOX.Lists",
        flags=["\\Seen"],
        discard=False,
        messages=[],
    )

    result = imap_session.execute(plan)

    assert result == type(result)()
    assert "select_folder" not in fake_imap.names()


# ----------------------------------------------------------------------------
def test_discard_deletes_and_never_moves(imap_session, fake_imap):
    fake_imap.messages = {1: message("a@example.com")}

    criteria = Criteria()
    criteria.add("from", "a@example.com")

    plan = imap_session.plan_actions(
        criteria, "INBOX", destination="INBOX.Lists", discard=True
    )
    result = imap_session.execute(plan)

    assert result.deleted == 1
    assert result.moved == 0
    assert "move" not in fake_imap.names()


# ----------------------------------------------------------------------------
def test_the_move_falls_back_to_copy_and_expunge_without_the_capability(
    imap_session, fake_imap
):
    """RFC 6851 MOVE is atomic; the fallback has to be exactly equivalent.

    UID EXPUNGE is used when UIDPLUS is advertised so a concurrent client's
    deleted mail is not expunged along with ours.
    """
    fake_imap.caps = {"UIDPLUS"}

    assert imap_session.move([1, 2], "INBOX.Lists") == 2

    assert ("copy", (1, 2), "INBOX.Lists") in fake_imap.calls
    assert ("add_flags", (1, 2), (b"\\Deleted",)) in fake_imap.calls
    assert ("uid_expunge", (1, 2)) in fake_imap.calls
    assert "expunge" not in fake_imap.names()


# ----------------------------------------------------------------------------
def test_the_move_fallback_uses_a_plain_expunge_without_uidplus(
    imap_session, fake_imap
):
    fake_imap.caps = set()

    imap_session.move([1], "INBOX.Lists")

    assert "expunge" in fake_imap.names()
    assert "uid_expunge" not in fake_imap.names()


# ----------------------------------------------------------------------------
def test_a_move_failure_names_the_destination(imap_session, fake_imap):
    fake_imap.failures["move"] = IMAPClientError("over quota")

    with pytest.raises(MxFilterError, match=r"move messages to 'INBOX.Lists'"):
        imap_session.move([1], "INBOX.Lists")


# ----------------------------------------------------------------------------
def test_moving_or_deleting_nothing_is_a_no_op(imap_session, fake_imap):
    assert imap_session.move([], "INBOX.Lists") == 0
    assert imap_session.delete([]) == 0
    assert fake_imap.calls == [("login", "user@example.com")]


# ############################################################################
# Plan reporting
# ############################################################################


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("source", "destination", "discard", "moves"),
    [
        pytest.param("INBOX", "INBOX.Lists", False, True, id="a-real-move"),
        pytest.param("INBOX", "", False, False, id="no-destination"),
        pytest.param("INBOX", "INBOX", False, False, id="same-folder"),
        pytest.param("INBOX", "inbox", False, False, id="same-folder-cased"),
        pytest.param("INBOX", "INBOX.Lists", True, False, id="discard-wins"),
    ],
)
def test_a_plan_knows_whether_it_relocates_mail(
    source, destination, discard, moves
):
    """The CLI's confirmation prompt escalates on this, so it must be right."""
    plan = MailActionPlan(
        source=source,
        destination=destination,
        flags=[],
        discard=discard,
        messages=[summary(1)],
    )

    assert plan.moves is moves


# ----------------------------------------------------------------------------
def test_a_plan_reports_its_size_and_uids():
    plan = MailActionPlan(
        source="INBOX",
        destination="",
        flags=[],
        discard=False,
        messages=[summary(3), summary(1)],
    )

    assert plan.count == 2
    assert plan.uids == [3, 1]
    assert plan.is_empty is False


# ----------------------------------------------------------------------------
def test_an_empty_plan_reports_itself_as_empty():
    plan = MailActionPlan(
        source="INBOX", destination="", flags=[], discard=False, messages=[]
    )

    assert plan.is_empty is True
    assert plan.count == 0
    assert plan.uids == []

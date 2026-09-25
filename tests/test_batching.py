"""Bulk IMAP commands are split into batches, and say so when they stop.

Two separate properties, and the second exists only because of the first.

**Batching.** ``IMAPClient`` joins a UID set with commas onto one command
line, so before this every bulk operation emitted a single command however
many messages it covered. Nothing capped that except ``--max-messages``,
which exists for an entirely different reason -- refusing to process a
partial batch silently -- and was therefore load-bearing by accident. The
first person to raise it for a big cleanup, which is the one thing the flag
is for, would have removed a protection nobody knew was there (issue #24).
So the tests below assert both halves of the fix: that a large set is
split, and that the split is not reached through the cap.

**Partial completion.** A batched operation is no longer atomic, so chunk
three of seven can fail with the first two already applied. That state was
previously unreachable and is now the interesting one: reporting it as a
plain failure would tell the user nothing happened when some of it did.
``PartialBatchError`` is what says otherwise, and these tests pin what it
says -- how many, which UIDs, and whether re-running is safe.
"""

import ast
from pathlib import Path

import pytest
from imapclient.exceptions import IMAPClientError

import mxfilter.imap
from mxfilter import MxFilterError
from mxfilter.cli import apply_to_existing, build_parser
from mxfilter.criteria import Criteria
from mxfilter.imap import (
    UID_BATCH_SIZE,
    MailActionPlan,
    MessageSummary,
    PartialBatchError,
    summarize_uids,
    uid_batches,
)

# ############################################################################
# Helpers
# ############################################################################

# The smallest thing the header re-check will accept as a message. Shared
# by every UID in a mailbox because these tests care how many commands were
# sent, never what was in them.
HEADERS = b"From: a@example.com\r\nSubject: s\r\n\r\n"


# ----------------------------------------------------------------------------
def uids(count: int, start: int = 1) -> list[int]:
    """A contiguous ascending UID list, the shape a search returns."""
    return list(range(start, start + count))


# ----------------------------------------------------------------------------
def mailbox(uid_list) -> dict[int, bytes]:
    """A fake mailbox holding one identical message per UID."""
    return dict.fromkeys(uid_list, HEADERS)


# ----------------------------------------------------------------------------
def batches_of(fake, name: str) -> list[tuple]:
    """Every UID tuple ``name`` was called with, in order."""
    return [call[1] for call in fake.calls if call[0] == name]


# ----------------------------------------------------------------------------
def plan_for(uid_list, **kwargs) -> MailActionPlan:
    """A plan over ``uid_list``, with no server round trip to build it."""
    return MailActionPlan(
        source=kwargs.pop("source", "INBOX"),
        destination=kwargs.pop("destination", "INBOX.Lists"),
        flags=kwargs.pop("flags", []),
        discard=kwargs.pop("discard", False),
        messages=[
            MessageSummary(
                uid=uid,
                date="",
                sender="a@example.com",
                subject="s",
                folder="INBOX",
            )
            for uid in uid_list
        ],
    )


# ############################################################################
# uid_batches
# ############################################################################


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("count", "size", "expected"),
    [
        (0, 3, []),
        (1, 3, [[1]]),
        (3, 3, [[1, 2, 3]]),
        (4, 3, [[1, 2, 3], [4]]),
        (7, 3, [[1, 2, 3], [4, 5, 6], [7]]),
    ],
)
def test_uid_batches_splits_without_losing_or_reordering(
    count, size, expected
):
    """Order is what makes a partial failure a describable prefix."""
    assert list(uid_batches(uids(count), size)) == expected


# ----------------------------------------------------------------------------
def test_uid_batches_refuses_a_size_that_would_never_finish():
    with pytest.raises(MxFilterError, match=r"at least 1"):
        list(uid_batches([1, 2, 3], 0))


# ----------------------------------------------------------------------------
def test_the_batch_size_fits_the_smallest_command_line_a_server_must_take():
    """RFC 2683 s3.2.1.5 asks servers to accept at least 8000 octets.

    A UID is at most ten digits plus a comma, so the worst-case UID set has
    to stay inside that with room for the tag and the command itself. This
    is the arithmetic the constant's comment states, kept as a test so a
    future "let's raise it" has to face the number rather than the vibe.
    """
    worst_case = UID_BATCH_SIZE * len("4294967295,")

    assert worst_case < 8000 - 200


# ----------------------------------------------------------------------------
def test_summarize_uids_stays_short_enough_for_an_error_message():
    assert summarize_uids([]) == "none"
    assert summarize_uids([1, 2, 3]) == "1, 2, 3"
    assert summarize_uids(uids(15)) == (
        "1, 2, 3, 4, 5, 6, 7, 8, 9, 10 and 5 more"
    )


# ############################################################################
# The operations are batched
# ############################################################################


# ----------------------------------------------------------------------------
def test_a_move_larger_than_one_batch_is_split(imap_session, fake_imap):
    """The regression test: this emitted exactly one command before the fix.

    A single MOVE carrying 1,500 comma-joined UIDs is a command line in the
    tens of kilobytes, which a server is entitled to reject -- and did not
    have to, only because --max-messages happened to cap it first.
    """
    everything = uids(UID_BATCH_SIZE * 2 + 300)

    assert imap_session.move(everything, "INBOX.Lists") == len(everything)

    sent = batches_of(fake_imap, "move")

    assert len(sent) == 3
    assert [len(batch) for batch in sent] == [
        UID_BATCH_SIZE,
        UID_BATCH_SIZE,
        300,
    ]
    assert [uid for batch in sent for uid in batch] == everything


# ----------------------------------------------------------------------------
def test_a_move_that_fits_in_one_batch_emits_exactly_one_command(
    imap_session, fake_imap
):
    """Batching must not turn the ordinary case into a series of commands."""
    imap_session.move(uids(5), "INBOX.Lists")

    assert batches_of(fake_imap, "move") == [(1, 2, 3, 4, 5)]


# ----------------------------------------------------------------------------
def test_flagging_is_batched(imap_session, fake_imap):
    everything = uids(UID_BATCH_SIZE + 1)

    imap_session.add_flags(everything, [b"\\Seen"])

    assert [len(batch) for batch in batches_of(fake_imap, "add_flags")] == [
        UID_BATCH_SIZE,
        1,
    ]


# ----------------------------------------------------------------------------
def test_deleting_batches_both_the_marking_and_the_expunge(
    imap_session, fake_imap
):
    everything = uids(UID_BATCH_SIZE + 1)

    assert imap_session.delete(everything) == len(everything)

    assert [len(batch) for batch in batches_of(fake_imap, "add_flags")] == [
        UID_BATCH_SIZE,
        1,
    ]
    assert [len(batch) for batch in batches_of(fake_imap, "uid_expunge")] == [
        UID_BATCH_SIZE,
        1,
    ]


# ----------------------------------------------------------------------------
def test_the_copy_fallback_batches_every_command_it_sends(
    imap_session, fake_imap
):
    """COPY, the \\Deleted marking, and UID EXPUNGE are all UID-carrying."""
    fake_imap.caps = {"UIDPLUS"}

    everything = uids(UID_BATCH_SIZE + 10)

    assert imap_session.move(everything, "INBOX.Lists") == len(everything)

    for name in ("copy", "add_flags", "uid_expunge"):
        assert [len(batch) for batch in batches_of(fake_imap, name)] == [
            UID_BATCH_SIZE,
            10,
        ], name


# ----------------------------------------------------------------------------
def test_a_server_without_uidplus_is_expunged_once_not_once_per_batch(
    imap_session, fake_imap
):
    """EXPUNGE takes no UID argument, so there is nothing to batch.

    Issuing it per batch would multiply the number of times a concurrent
    client's own deleted mail could be swept up -- a real cost, paid for no
    benefit, since one EXPUNGE at the end removes exactly the same messages.
    """
    fake_imap.caps = set()

    imap_session.move(uids(UID_BATCH_SIZE * 2), "INBOX.Lists")

    assert fake_imap.names().count("expunge") == 1


# ----------------------------------------------------------------------------
def test_the_header_recheck_fetch_is_batched(imap_session, fake_imap):
    """The candidate set is the server's, and no cap in this tool bounds it.

    ``--max-messages`` is consulted after the search, so a folder with
    thousands of matches puts every one of their UIDs on the FETCH line
    before the cap is ever read. That is the same defect one layer up.
    """
    fake_imap.messages = mailbox(uids(UID_BATCH_SIZE + 5))

    criteria = Criteria()
    criteria.add("from", "a@example.com")

    found = imap_session.search(criteria, "INBOX")

    assert len(found) == UID_BATCH_SIZE + 5
    assert [len(batch) for batch in batches_of(fake_imap, "fetch")] == [
        UID_BATCH_SIZE,
        5,
    ]


# ############################################################################
# Batching is independent of --max-messages
# ############################################################################


# ----------------------------------------------------------------------------
def test_the_transport_layer_never_reads_the_policy_cap():
    """The structural half of the fix, and the one that has to keep holding.

    ``--max-messages`` is a policy ceiling the user sets on how much mail to
    touch. Batching is a transport detail they should never think about.
    The two were entangled by accident once; an AST walk is what stops them
    being entangled again on purpose. Comments are free to name the flag --
    the constant's own comment does, at length -- because ``ast`` does not
    see them.
    """
    source = Path(mxfilter.imap.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    mentions = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and "max_message" in node.id
    } | {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and "max_message" in node.attr
    }

    assert mentions == set(), (
        f"mxfilter/imap.py reads {sorted(mentions)}; the batch size is a "
        f"transport detail and must never be derived from the cap (#24)"
    )


# ----------------------------------------------------------------------------
@pytest.mark.parametrize("cap", [1500, 5000, 100_000])
def test_raising_the_cap_no_longer_removes_the_protection(
    imap_session, fake_imap, capsys, cap
):
    """The defect, stated as a test, at the level the user meets it.

    Raising ``--max-messages`` for a big cleanup is the one thing the flag
    exists for, and before this it silently removed the only thing keeping
    the command line to a sane length. Now the same run is split the same
    way whatever the cap says.
    """
    matched = uids(1500)

    fake_imap.messages = mailbox(matched)

    args = build_parser().parse_args(
        [
            "apply",
            "--from",
            "a@example.com",
            "--fileinto",
            "Lists",
            "--yes",
            "--max-messages",
            str(cap),
        ]
    )

    criteria = Criteria()
    criteria.add("from", "a@example.com")

    apply_to_existing(imap_session, criteria, args, "INBOX.Lists")

    capsys.readouterr()

    sent = batches_of(fake_imap, "move")

    assert [len(batch) for batch in sent] == [
        UID_BATCH_SIZE,
        UID_BATCH_SIZE,
        1500 - UID_BATCH_SIZE * 2,
    ]
    assert [uid for batch in sent for uid in batch] == matched


# ----------------------------------------------------------------------------
def test_lowering_the_cap_still_refuses_the_whole_run(
    imap_session, fake_imap, capsys
):
    """The cap keeps its own job, unchanged and unshared.

    Batching must not have quietly turned the ceiling into "do the first N
    and stop" -- refusing the whole thing is the behaviour the cap was
    written for, and the reason it was the wrong tool for line length.
    """
    fake_imap.messages = mailbox(uids(30))

    args = build_parser().parse_args(
        [
            "apply",
            "--from",
            "a@example.com",
            "--fileinto",
            "Lists",
            "--yes",
            "--max-messages",
            "10",
        ]
    )

    criteria = Criteria()
    criteria.add("from", "a@example.com")

    with pytest.raises(MxFilterError, match=r"NO existing message"):
        apply_to_existing(imap_session, criteria, args, "INBOX.Lists")

    capsys.readouterr()

    assert "move" not in fake_imap.names()


# ############################################################################
# Partial completion
# ############################################################################


# ----------------------------------------------------------------------------
def test_a_move_that_stops_half_way_reports_what_got_through(
    imap_session, fake_imap
):
    """The state batching newly makes reachable, and what it must say."""
    fake_imap.fail_after["move"] = (2, IMAPClientError("over quota"))

    everything = uids(UID_BATCH_SIZE * 3)

    with pytest.raises(PartialBatchError) as caught:
        imap_session.move(everything, "INBOX.Lists")

    exc = caught.value

    assert exc.completed == tuple(everything[: UID_BATCH_SIZE * 2])
    assert exc.remaining == tuple(everything[UID_BATCH_SIZE * 2 :])
    assert exc.duplicated == ()
    assert exc.total == len(everything)

    text = str(exc)

    assert f"{UID_BATCH_SIZE * 2} of {len(everything)}" in text
    assert "over quota" in text
    assert f"UID at or below {UID_BATCH_SIZE * 2}" in text
    assert "Re-running the same command is safe" in text


# ----------------------------------------------------------------------------
def test_a_partial_failure_never_says_the_word_batch(imap_session, fake_imap):
    """How the work was split up is not the user's problem.

    The whole point of doing this below the cap is that the user never has
    to think about it. A report that explains itself in chunks hands the
    transport detail straight back to them.
    """
    fake_imap.fail_after["move"] = (1, IMAPClientError("connection reset"))

    with pytest.raises(PartialBatchError) as caught:
        imap_session.move(uids(UID_BATCH_SIZE * 2), "INBOX.Lists")

    lowered = str(caught.value).lower()

    assert "batch" not in lowered
    assert "chunk" not in lowered


# ----------------------------------------------------------------------------
def test_a_first_batch_failure_is_still_an_ordinary_failure(
    imap_session, fake_imap
):
    """Nothing happened, so nothing partial should be claimed.

    Batching should not change the wording of the failure that was already
    possible before it -- a move that achieves nothing is the same event it
    always was.
    """
    fake_imap.failures["move"] = IMAPClientError("over quota")

    with pytest.raises(MxFilterError) as caught:
        imap_session.move(uids(UID_BATCH_SIZE * 2), "INBOX.Lists")

    assert not isinstance(caught.value, PartialBatchError)
    assert "could not move messages to 'INBOX.Lists'" in str(caught.value)


# ----------------------------------------------------------------------------
def test_a_partial_flagging_reports_itself(imap_session, fake_imap):
    fake_imap.fail_after["add_flags"] = (1, IMAPClientError("no permission"))

    with pytest.raises(PartialBatchError) as caught:
        imap_session.add_flags(uids(UID_BATCH_SIZE * 2), [b"\\Seen"])

    exc = caught.value

    assert exc.operation == "flag"
    assert len(exc.completed) == UID_BATCH_SIZE
    assert len(exc.remaining) == UID_BATCH_SIZE
    assert "were flagged" in str(exc)


# ----------------------------------------------------------------------------
def test_a_partial_delete_expunges_what_it_already_marked(
    imap_session, fake_imap
):
    """Leaving them marked but not removed is what would make a re-run lie.

    The messages are already flagged \\Deleted, so a later expunge by any
    client drops them. Finishing the removal here is what makes "re-running
    is safe" true rather than nearly true.
    """
    fake_imap.fail_after["add_flags"] = (1, IMAPClientError("timeout"))

    with pytest.raises(PartialBatchError) as caught:
        imap_session.delete(uids(UID_BATCH_SIZE * 2))

    exc = caught.value

    assert exc.operation == "delete"
    assert len(exc.completed) == UID_BATCH_SIZE
    assert batches_of(fake_imap, "uid_expunge") == [
        tuple(uids(UID_BATCH_SIZE))
    ]
    assert "Re-running the same command is safe" in str(exc)


# ----------------------------------------------------------------------------
def test_a_copy_that_could_not_be_removed_is_reported_as_a_duplicate(
    imap_session, fake_imap
):
    """The one state where re-running would make things worse.

    On a server with no MOVE, a batch is copied and then marked. If the
    copy lands and the marking does not, that mail is in both folders --
    so the advice has to be the opposite of the usual one, and has to name
    the messages.
    """
    fake_imap.caps = {"UIDPLUS"}
    fake_imap.fail_after["add_flags"] = (1, IMAPClientError("timeout"))

    everything = uids(UID_BATCH_SIZE * 3)

    with pytest.raises(PartialBatchError) as caught:
        imap_session.move(everything, "INBOX.Lists")

    exc = caught.value

    assert exc.completed == tuple(everything[:UID_BATCH_SIZE])
    assert exc.duplicated == tuple(
        everything[UID_BATCH_SIZE : UID_BATCH_SIZE * 2]
    )
    assert exc.remaining == tuple(everything[UID_BATCH_SIZE * 2 :])

    text = str(exc)

    assert "WARNING" in text
    assert "now in both" in text
    assert "Re-running the same command is safe" not in text

    # The batch that did complete was still expunged, so it really moved.
    assert batches_of(fake_imap, "uid_expunge") == [
        tuple(everything[:UID_BATCH_SIZE])
    ]


# ----------------------------------------------------------------------------
def test_a_failed_expunge_means_nothing_moved_at_all(imap_session, fake_imap):
    """Copy plus mark is not a move until the expunge lands."""
    fake_imap.caps = {"UIDPLUS"}
    fake_imap.failures["uid_expunge"] = IMAPClientError("busy")

    with pytest.raises(PartialBatchError) as caught:
        imap_session.move(uids(UID_BATCH_SIZE * 2), "INBOX.Lists")

    exc = caught.value

    assert exc.completed == ()
    assert len(exc.duplicated) == UID_BATCH_SIZE * 2
    assert "WARNING" in str(exc)


# ############################################################################
# execute() completes the report
# ############################################################################


# ----------------------------------------------------------------------------
def test_execute_names_the_source_folder_on_a_partial_move(
    imap_session, fake_imap
):
    """Only the plan knows where the mail came from."""
    fake_imap.fail_after["move"] = (1, IMAPClientError("over quota"))

    plan = plan_for(uids(UID_BATCH_SIZE * 2), source="INBOX.Lists.Old")

    with pytest.raises(PartialBatchError) as caught:
        imap_session.execute(plan)

    assert caught.value.source == "INBOX.Lists.Old"
    assert "from 'INBOX.Lists.Old' to 'INBOX.Lists'" in str(caught.value)


# ----------------------------------------------------------------------------
def test_execute_reports_the_flagging_that_did_finish(imap_session, fake_imap):
    """Flags are applied before the move, so they can have fully succeeded.

    Saying only that the move failed would understate what changed, and the
    user would go looking for unflagged mail that is not there.
    """
    fake_imap.fail_after["move"] = (1, IMAPClientError("over quota"))

    everything = uids(UID_BATCH_SIZE * 2)
    plan = plan_for(everything, flags=["\\Seen"])

    with pytest.raises(PartialBatchError) as caught:
        imap_session.execute(plan)

    exc = caught.value

    assert exc.result.flagged == len(everything)
    assert exc.result.moved == UID_BATCH_SIZE
    assert f"Flags were applied to all {len(everything)}" in str(exc)


# ----------------------------------------------------------------------------
def test_execute_reports_a_partial_discard(imap_session, fake_imap):
    fake_imap.fail_after["add_flags"] = (1, IMAPClientError("timeout"))

    plan = plan_for(uids(UID_BATCH_SIZE * 2), discard=True)

    with pytest.raises(PartialBatchError) as caught:
        imap_session.execute(plan)

    exc = caught.value

    assert exc.operation == "delete"
    assert exc.result.deleted == UID_BATCH_SIZE
    assert exc.source == "INBOX"

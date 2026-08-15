"""The "show" half of show-then-change, for the two things it must show.

Every mutating subcommand works out what would change, shows it, and only
then changes it (CONVENTIONS.md). Both halves of that showing step are
covered here because both fail the same way -- silently, by looking
plausible:

* **Which message a rule came from.** ``from-message`` takes a UID the
  operator read out of webmail by hand. The derived criteria are equally
  plausible whichever message produced them, so criteria alone can never
  catch a mistyped digit; only the message's own headers can. And the
  command that follows moves mail.
* **What the diff is really saying.** A merge re-renders the whole script
  through sievelib, so a diff against the server's raw copy reports the
  renderer's indentation as though it were the change -- 29 moved lines
  for a no-op round trip on a 25-line script. "Everything changed because
  of whitespace" and "everything changed because something went wrong"
  render identically, which is a diff that cannot be approved.

The second half also carries an obligation the first does not: the
reformat is real, so normalising the diff must **report** it rather than
merely hide it (issue #37).
"""

import email

import pytest

from mxfilter.cli import print_message, print_script_diff
from mxfilter.criteria import Criteria
from mxfilter.sieve import (
    display_diff,
    merge_rule,
    remove_rule,
    script_diff,
)

# ############################################################################
# Helpers
# ############################################################################


# ----------------------------------------------------------------------------
def message(**headers):
    """Build a header-only message from ``name=value`` pairs.

    Header order follows the keyword order, and a value of None omits the
    header entirely -- which is how the absent-header cases are written
    without hand-assembling a second blob of bytes.
    """
    lines = [
        f"{name.replace('_', '-')}: {value}"
        for name, value in headers.items()
        if value is not None
    ]

    raw = ("\n".join(lines) + "\n\nbody\n").encode("utf-8")

    return email.message_from_bytes(raw)


# ----------------------------------------------------------------------------
def merge_simple(existing: str, name: str, folder: str) -> str:
    """Merge a plain 'file it here and stop' rule into ``existing``."""
    criteria = Criteria()
    criteria.add("from", "a@example.com")

    return merge_rule(
        existing,
        name,
        criteria.sieve_conditions(),
        [("fileinto", folder), ("stop",)],
        criteria.sieve_matchtype(),
    )


# ----------------------------------------------------------------------------
def removed_lines(diff: str) -> list[str]:
    """Return the diff's removal lines, minus the ``---`` file header."""
    return [
        line
        for line in diff.splitlines()
        if line.startswith("-") and not line.startswith("---")
    ]


# ############################################################################
# Which message the rule came from
# ############################################################################


# ----------------------------------------------------------------------------
def test_the_message_block_names_the_uid_and_folder_it_read(capsys):
    """The heading has to repeat what was asked for, not just answer it.

    ``--search`` picks the UID itself, and warns only when more than one
    message matched -- so the uid on screen is often the first time the
    operator sees which message the command settled on.
    """
    print_message(message(From="boss@example.com"), 813, "INBOX")

    assert "Message uid 813 in 'INBOX':" in capsys.readouterr().out


# ----------------------------------------------------------------------------
def test_the_message_block_shows_the_headers_a_reader_recognises_it_by(
    capsys,
):
    """Date, From, To and Subject, each with its value."""
    print_message(
        message(
            Date="Thu, 14 Aug 2026 09:12:03 -0600",
            From="Optum Financial <OF-Service@of.optum.com>",
            To="harleypig@example.com",
            Subject="Your monthly statement is ready",
        ),
        813,
        "INBOX",
    )

    out = capsys.readouterr().out

    assert "Date:    Thu, 14 Aug 2026 09:12:03 -0600" in out
    assert "From:    Optum Financial <OF-Service@of.optum.com>" in out
    assert "To:      harleypig@example.com" in out
    assert "Subject: Your monthly statement is ready" in out


# ----------------------------------------------------------------------------
def test_a_list_id_is_shown_because_auto_derives_from_it(capsys):
    """``--derive auto`` prefers List-Id, so it cannot be the hidden one.

    A rule derived from a header the operator was never shown is a rule
    they cannot check -- the exact gap this display exists to close.
    """
    print_message(
        message(
            From="notifications@github.com",
            List_Id="harleypig/dotagents <dotagents.harleypig.github.com>",
        ),
        42,
        "INBOX",
    )

    out = capsys.readouterr().out

    assert "List-Id: harleypig/dotagents" in out


# ----------------------------------------------------------------------------
def test_an_encoded_subject_is_decoded_for_the_reader(capsys):
    """RFC 2047 encoded words are unreadable as verification.

    The raw form is what the wire carries; nobody recognises their own
    mail from ``=?utf-8?q?...?=``, so this is decoded exactly as the
    criteria derived from it are.
    """
    print_message(
        message(Subject="=?utf-8?q?Facture_pay=C3=A9e?="),
        7,
        "INBOX",
    )

    out = capsys.readouterr().out

    assert "Subject: Facture payée" in out
    assert "=?utf-8?q?" not in out


# ----------------------------------------------------------------------------
def test_a_folded_header_is_collapsed_onto_one_line(capsys):
    """A long Subject arrives wrapped, with the continuation indented.

    Printed as received it would break the aligned block into ragged
    fragments, which is precisely the scan this display is for.
    """
    print_message(
        message(Subject="[harleypig/dotagents] a subject\n that was folded"),
        7,
        "INBOX",
    )

    out = capsys.readouterr().out

    assert "Subject: [harleypig/dotagents] a subject that was folded" in out


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "absent",
    [
        pytest.param("To", id="no-to"),
        pytest.param("List_Id", id="no-list-id"),
        pytest.param("Date", id="no-date"),
    ],
)
def test_a_missing_header_is_left_out_rather_than_crashing(absent, capsys):
    """An absent To, Date or List-Id is ordinary, not an error.

    Automated mail routinely omits one of these, and a message this
    command cannot display is a message the operator cannot verify --
    so the block degrades rather than fails, and prints no "(none)" row
    to clutter what is left.
    """
    headers = {
        "Date": "Thu, 14 Aug 2026 09:12:03 -0600",
        "To": "harleypig@example.com",
        "From": "boss@example.com",
        "Subject": "still identifiable",
        "List_Id": "<list.example.com>",
    }
    headers[absent] = None

    print_message(message(**headers), 5, "INBOX")

    out = capsys.readouterr().out

    assert "From:    boss@example.com" in out
    assert "Subject: still identifiable" in out
    assert f"{absent.replace('_', '-')}:" not in out


# ----------------------------------------------------------------------------
def test_a_message_with_no_headers_at_all_still_prints_its_heading(capsys):
    """The degenerate case, so an empty fetch is a thin block not a crash."""
    print_message(email.message_from_bytes(b"\r\n"), 1, "INBOX")

    assert capsys.readouterr().out == "Message uid 1 in 'INBOX':\n"


# ----------------------------------------------------------------------------
def test_a_pathological_header_is_clipped(capsys):
    """A header is attacker-supplied text; it does not get the whole screen.

    Clipping is a last resort here rather than a layout choice -- the
    width is set well above anything a real Subject reaches, because
    truncating the value being verified is self-defeating.
    """
    print_message(message(Subject="x" * 5000), 9, "INBOX")

    line = capsys.readouterr().out.splitlines()[1]

    assert len(line) < 120
    assert line.endswith("…")


# ############################################################################
# What the diff is really saying
# ############################################################################


# ----------------------------------------------------------------------------
def test_the_diff_against_a_roundcube_script_shows_only_the_new_rule(
    roundcube_script,
):
    """Issue #37, the measurement that prompted it.

    The raw diff moves every line of a hand-written script because
    sievelib re-indents it. Normalising both sides leaves the added rule
    and nothing else -- so there is no removal line at all, which is the
    strongest form of "only the real change shows".
    """
    after = merge_simple(roundcube_script, "new-rule", "INBOX.New")

    report = display_diff(roundcube_script, after, "active")

    assert "+# rule:[new-rule]" in report.text
    assert removed_lines(report.text) == []
    assert "keep-boss" not in report.text


# ----------------------------------------------------------------------------
def test_the_reformat_is_reported_rather_than_only_hidden(roundcube_script):
    """Normalising alone would trade one silent surprise for another.

    The upload really does rewrite the file in sievelib's layout. A diff
    that shows none of that, and says nothing about it either, has hidden
    a change the server is about to make.
    """
    after = merge_simple(roundcube_script, "new-rule", "INBOX.New")

    assert display_diff(roundcube_script, after, "active").reformats


# ----------------------------------------------------------------------------
def test_an_already_normalised_script_reports_no_reformat(roundcube_script):
    """The note has to stop, and this is the merge after which it does.

    Once mxfilter has uploaded once, the server's copy is already in this
    formatting. A note that appeared every run would be read as
    boilerplate and stop being read at all -- so it is keyed on the real
    difference, not on the command.
    """
    first = merge_simple(roundcube_script, "new-rule", "INBOX.New")

    report = display_diff(first, merge_simple(first, "later", "INBOX.Later"))

    assert not report.reformats
    assert "+# rule:[later]" in report.text


# ----------------------------------------------------------------------------
def test_an_empty_script_is_not_reported_as_a_reformat():
    """A first run has nothing to reformat, and must not claim otherwise.

    An account with no script at all is the one case where ``before`` is
    empty; rendering an empty filter set gives back an empty string, so
    the flag stays false without a special case to keep it there.
    """
    report = display_diff("", merge_simple("", "first", "INBOX.First"))

    assert not report.reformats
    assert "+# rule:[first]" in report.text


# ----------------------------------------------------------------------------
def test_a_removal_diff_is_normalised_too(roundcube_script):
    """``remove-rule`` shares the failure, so it shares the fix.

    Its diff is produced the same way -- parse, drop a rule, re-render --
    so against a hand-written script it moved every line for the same
    reason ``add`` did.
    """
    after = remove_rule(roundcube_script, "keep-boss")

    report = display_diff(roundcube_script, after, "active")

    assert report.reformats
    assert "-# rule:[keep-boss]" in report.text
    assert "bin-the-noise" not in removed_lines(report.text)


# ----------------------------------------------------------------------------
def test_script_diff_still_returns_the_raw_unnormalised_diff(
    roundcube_script,
):
    """Normalisation is layered on top; it did not leak into the helper.

    ``script_diff`` is the plain two-texts differ, and what it is handed
    is the caller's business. Folding a parse-and-render into it would
    change what every caller gets -- including a comparison of two files
    where re-rendering is exactly wrong.
    """
    after = merge_simple(roundcube_script, "new-rule", "INBOX.New")

    raw = script_diff(roundcube_script, after, "active")

    assert removed_lines(raw), "the raw diff should still show the churn"
    assert '-\tfileinto "INBOX.Boss";' in raw


# ############################################################################
# How the diff reaches the reader
# ############################################################################


# ----------------------------------------------------------------------------
def test_the_note_appears_when_the_server_copy_will_be_reformatted(
    roundcube_script, capsys
):
    """What the reader is told, in the words they are told it in."""
    after = merge_simple(roundcube_script, "new-rule", "INBOX.New")

    print_script_diff(display_diff(roundcube_script, after, "active"))

    out = capsys.readouterr().out

    assert "re-indents the whole file" in out
    assert "no rule body is altered" in out
    assert "+# rule:[new-rule]" in out


# ----------------------------------------------------------------------------
def test_the_note_is_absent_once_the_script_is_already_normalised(capsys):
    """The steady state: every run after the first says nothing extra."""
    first = merge_simple("", "first", "INBOX.First")

    print_script_diff(
        display_diff(first, merge_simple(first, "later", "INBOX.Later"))
    )

    out = capsys.readouterr().out

    assert "re-indents" not in out
    assert "+# rule:[later]" in out


# ----------------------------------------------------------------------------
def test_no_change_is_said_in_words_rather_than_shown_as_an_empty_diff(
    capsys,
):
    """A zero-line patch reads as a bug; "(no change)" reads as an answer."""
    script = merge_simple("", "one", "INBOX.One")

    print_script_diff(display_diff(script, script))

    assert "(no change)" in capsys.readouterr().out

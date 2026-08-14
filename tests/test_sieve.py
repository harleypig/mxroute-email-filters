"""Offline script editing: parse, merge, remove, render, diff, back up.

The merge tests are the reason this file exists. An MXroute account's
active script is the same file Roundcube's filter UI writes, so a merge
that loses a rule destroys work the user did by hand, and does it
silently -- PUTSCRIPT succeeds and the loss only surfaces days later
(ADR 0002). Everything below either proves that cannot happen or proves
the failure is loud.
"""

import pytest

from mxfilter import MxFilterError
from mxfilter import sieve as sieve_module
from mxfilter.criteria import Criteria, escape_sieve_string
from mxfilter.sieve import (
    backup_script,
    merge_rule,
    parse_script,
    remove_rule,
    render_script,
    rule_names,
    script_diff,
)

# ############################################################################
# Helpers
# ############################################################################


# ----------------------------------------------------------------------------
def simple_criteria(header: str = "from", value: str = "a@example.com"):
    """Return a one-term Criteria, for tests that only need conditions."""
    criteria = Criteria()
    criteria.add(header, value)

    return criteria


# ----------------------------------------------------------------------------
def merge_simple(existing: str, name: str, folder: str, **kwargs) -> str:
    """Merge a plain 'file it here and stop' rule into ``existing``."""
    criteria = simple_criteria()

    return merge_rule(
        existing,
        name,
        criteria.sieve_conditions(),
        [("fileinto", folder), ("stop",)],
        criteria.sieve_matchtype(),
        **kwargs,
    )


# ############################################################################
# The data-loss guard
# ############################################################################


# ----------------------------------------------------------------------------
def test_merge_keeps_every_pre_existing_rule(roundcube_script):
    """The single most important assertion in the suite.

    A merge must carry through rules mxfilter did not write. The check is
    on the rule *bodies* -- the conditions and the actions -- because that
    is what actually sorts the user's mail; a rule whose fileinto target
    survived is a rule that still works.
    """
    merged = merge_simple(roundcube_script, "new-rule", "INBOX.New")

    # The hand-made rules' conditions.
    assert 'header :contains "from" "boss@example.com"' in merged
    assert 'header :contains "subject" "newsletter"' in merged

    # And their actions, unchanged -- including the flag, which is where a
    # naive re-render would drop a backslash.
    assert 'fileinto "INBOX.Boss";' in merged
    assert 'setflag "\\\\Flagged";' in merged
    assert 'fileinto "INBOX.Noise";' in merged

    # The new rule landed as well, so this is a merge and not a no-op.
    assert 'fileinto "INBOX.New";' in merged


# ----------------------------------------------------------------------------
def test_merged_script_reparses_with_sievelib(roundcube_script, reparse):
    """Emitting text that sievelib cannot read back would strand the user.

    The next run parses the active script before merging into it, so an
    unparseable emission turns every later ``mxfilter add`` into a hard
    stop against a script only mxfilter could have written.
    """
    merged = merge_simple(roundcube_script, "new-rule", "INBOX.New")

    reparse(merged)

    assert rule_names(parse_script(merged))[-1] == "new-rule"


# ----------------------------------------------------------------------------
def test_merge_does_not_reorder_the_existing_rules(roundcube_script):
    """Sieve is order-sensitive: the first matching rule with ``stop`` wins.

    Reordering would silently change which rule fires for a message that
    two rules both match, so appending at the end is part of the contract,
    not an implementation detail.
    """
    merged = merge_simple(roundcube_script, "new-rule", "INBOX.New")

    boss = merged.index("boss@example.com")
    noise = merged.index("newsletter")
    new = merged.index("INBOX.New")

    assert boss < noise < new


# ----------------------------------------------------------------------------
def test_merge_keeps_the_require_line_correct(roundcube_script):
    """The union of every rule's needs, not just the new rule's.

    ``require`` is re-derived on each render. Losing an extension another
    rule depends on makes the whole script fail to load server-side.
    """
    merged = merge_simple(roundcube_script, "new-rule", "INBOX.New")
    require_line = merged.splitlines()[0]

    assert "fileinto" in require_line
    assert "imap4flags" in require_line


# ############################################################################
# Foreign rule identity
# ############################################################################


# ----------------------------------------------------------------------------
def test_merge_preserves_a_roundcube_rule_name(roundcube_script):
    """Rule *names* are part of what has to survive, not just bodies.

    Roundcube's UI keys on ``# rule:[name]``; after one merge the user's
    filter list shows 'Unnamed rule 1'. Worse, the name is what mxfilter
    itself uses for identity, so the rename also breaks --replace and
    remove-rule against that rule (see the next test).
    """
    merged = merge_simple(roundcube_script, "new-rule", "INBOX.New")

    assert "keep-boss" in rule_names(parse_script(merged))


# ----------------------------------------------------------------------------
def test_replace_updates_a_roundcube_named_rule_in_place(roundcube_script):
    """The silent half of the identity defect.

    ``--replace`` on a name Roundcube wrote used to report success and leave
    two rules matching the same mail. The earlier one carries ``stop``, so
    the replacement never fired and the user saw a command that "worked" and
    changed nothing.
    """
    merged = merge_simple(
        roundcube_script, "keep-boss", "INBOX.Elsewhere", replace=True
    )

    assert merged.count("# rule:[") == 2
    assert 'fileinto "INBOX.Boss";' not in merged
    assert 'fileinto "INBOX.Elsewhere";' in merged


# ############################################################################
# Name-marker translation
# ############################################################################


# ----------------------------------------------------------------------------
def test_render_writes_roundcube_name_markers(roundcube_script):
    """The panel is the other editor of this file, so it sets the dialect.

    Emitting sievelib's ``# Filter:`` would leave every rule mxfilter writes
    nameless in the webmail UI -- the same identity loss as the parse bug,
    pointed the other way.
    """
    merged = merge_simple(roundcube_script, "new-rule", "INBOX.New")

    assert "# rule:[new-rule]" in merged
    assert "# rule:[keep-boss]" in merged
    assert "# Filter:" not in merged


# ----------------------------------------------------------------------------
def test_names_do_not_drift_over_two_render_cycles(roundcube_script):
    """Translation has to be a fixed point, not a one-way conversion.

    Every run re-parses what the previous run rendered, so a name that
    shifts by one cycle -- gaining a bracket, losing a prefix -- diverges a
    little further on each ``mxfilter add`` until identity is lost anyway.
    """
    first = render_script(parse_script(roundcube_script))
    second = render_script(parse_script(first))
    third = render_script(parse_script(second))

    assert second == first
    assert third == first
    assert rule_names(parse_script(third)) == ["keep-boss", "bin-the-noise"]


# ----------------------------------------------------------------------------
def test_both_dialects_are_read_from_one_script(reparse):
    """A script mxfilter and Roundcube have both edited carries both forms."""
    script = (
        'require ["fileinto"];\n'
        "# rule:[from-the-panel]\n"
        'if header :contains "from" "a@example.com" {\n'
        '    fileinto "INBOX.A";\n'
        "}\n"
        "# Filter: from-mxfilter\n"
        'if header :contains "from" "b@example.com" {\n'
        '    fileinto "INBOX.B";\n'
        "}\n"
    )

    reparse(script)

    assert rule_names(parse_script(script)) == [
        "from-the-panel",
        "from-mxfilter",
    ]


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        pytest.param("# rule:[plain]", "plain", id="one-space"),
        pytest.param("#rule:[tight]", "tight", id="no-space"),
        pytest.param("#   rule:[roomy]", "roomy", id="several-spaces"),
        pytest.param("#\trule:[tabbed]", "tabbed", id="tab"),
        pytest.param("# rule:[trailing]   ", "trailing", id="trailing-space"),
        pytest.param("# rule:[a]b]", "a]b", id="bracket-in-name"),
        pytest.param("# rule:[rule:[z]]", "rule:[z]", id="marker-in-name"),
        pytest.param("# rule:[Filter: x]", "Filter: x", id="other-dialect"),
        pytest.param("# rule:[naïve]", "naïve", id="non-ascii"),
    ],
)
def test_a_name_marker_variant_is_read_and_round_trips(marker, expected):
    """The name has to survive whatever Roundcube or a human wrote.

    A ``]`` inside the name is the interesting one: reading has to run to
    the *last* bracket, or the name comes back truncated and the rule
    answers to a name nobody typed. The non-ASCII case pins the byte-offset
    arithmetic the rewrite splices on.
    """
    script = (
        'require ["fileinto"];\n'
        f"{marker}\n"
        'if header :contains "from" "a@example.com" {\n'
        '    fileinto "INBOX.A";\n'
        "}\n"
    )

    assert rule_names(parse_script(script)) == [expected]

    rendered = render_script(parse_script(script))

    assert rule_names(parse_script(rendered)) == [expected]


# ----------------------------------------------------------------------------
def test_a_rule_with_no_name_marker_stays_unnamed():
    """Inventing a name for an unnamed rule would be a different bug."""
    script = (
        'require ["fileinto"];\n'
        "# just a note, not a name\n"
        'if header :contains "from" "a@example.com" {\n'
        '    fileinto "INBOX.A";\n'
        "}\n"
    )

    assert rule_names(parse_script(script)) == ["Unnamed rule 1"]


# ----------------------------------------------------------------------------
def test_a_crlf_script_keeps_its_names(roundcube_script, reparse):
    """ManageSieve is a CRLF protocol, so this is the wire form.

    The lexer hands the carriage return over as part of the comment, so a
    rewrite that forgets it either drops the line ending or reads the name
    as ``keep-boss\\r``.
    """
    script = roundcube_script.replace("\n", "\r\n")

    reparse(script)

    assert rule_names(parse_script(script)) == ["keep-boss", "bin-the-noise"]


# ----------------------------------------------------------------------------
NOT_A_MARKER = [
    pytest.param(
        'if header :contains "subject" "# rule:[in-a-string]" {\n'
        '    fileinto "INBOX.A";\n'
        "}\n",
        id="string-literal",
    ),
    pytest.param(
        "# a note about # rule:[mid-comment] and what it does\n"
        'if header :contains "from" "a@example.com" {\n'
        '    fileinto "INBOX.A";\n'
        "}\n",
        id="comment-body",
    ),
    pytest.param(
        "/* # rule:[in-a-bracket-comment] */\n"
        'if header :contains "from" "a@example.com" {\n'
        '    fileinto "INBOX.A";\n'
        "}\n",
        id="bracket-comment",
    ),
]


# ----------------------------------------------------------------------------
@pytest.mark.parametrize("body", NOT_A_MARKER)
def test_a_marker_that_is_not_a_marker_names_nothing(body, reparse):
    """Only a comment that *is* the marker names a rule.

    Reading the name off any line that happens to contain the text would
    rename a rule from inside its own subject test -- and, worse, rewrite
    the user's string when the script is rendered back.
    """
    script = f'require ["fileinto"];\n{body}'

    reparse(script)

    assert rule_names(parse_script(script)) == ["Unnamed rule 1"]


# ----------------------------------------------------------------------------
def test_a_marker_inside_a_multiline_block_is_left_verbatim(reparse):
    """A ``text:`` block is message content, not script structure."""
    script = (
        'require ["reject"];\n'
        "# rule:[bounce-it]\n"
        'if header :contains "subject" "x" {\n'
        "    reject text:\n"
        "# rule:[part-of-the-message]\n"
        ".\n"
        ";\n"
        "}\n"
    )

    assert rule_names(parse_script(script)) == ["bounce-it"]

    rendered = render_script(parse_script(script))

    reparse(rendered)

    assert "# rule:[part-of-the-message]\n" in rendered
    assert rule_names(parse_script(rendered)) == ["bounce-it"]


# ----------------------------------------------------------------------------
def test_a_roundcube_name_collides_without_replace(roundcube_script):
    """Collision detection is the same identity, seen from the add path.

    Before the name survived parsing this raised nothing and appended a
    second rule -- the silent failure, since the first one carries ``stop``.
    """
    with pytest.raises(MxFilterError, match=r"already exists.*--replace"):
        merge_simple(roundcube_script, "keep-boss", "INBOX.Elsewhere")


# ----------------------------------------------------------------------------
def test_a_roundcube_named_rule_can_be_removed(roundcube_script, reparse):
    """``remove-rule keep-boss`` used to fail with 'Known rules: Unnamed…'."""
    after = remove_rule(roundcube_script, "keep-boss")

    reparse(after)

    assert rule_names(parse_script(after)) == ["bin-the-noise"]
    assert 'fileinto "INBOX.Boss";' not in after
    assert 'fileinto "INBOX.Noise";' in after


# ----------------------------------------------------------------------------
def test_a_misplaced_comment_offset_fails_loudly(
    monkeypatch, roundcube_script
):
    """The name rewrite splices bytes, so a wrong offset corrupts a script.

    The offsets come from sievelib's lexer. If a future version moved them,
    silently rewriting the wrong bytes would be worse than any name loss --
    so the splice is checked and the failure is named, the same posture as
    ADR 0002's parse hard stop.
    """

    class DriftingLexer:
        """A lexer that reports an offset the token is not actually at."""

        def __init__(self, definitions):
            self.pos = 0

        def scan(self, text):
            self.pos = 1

            yield ("hash_comment", b"# rule:[somewhere-else]")

    monkeypatch.setattr(sieve_module.parser, "Lexer", DriftingLexer)

    with pytest.raises(MxFilterError, match="version mismatch"):
        parse_script(roundcube_script)


# ----------------------------------------------------------------------------
def test_a_roundcube_name_survives_repeated_merges(roundcube_script):
    """The defect only showed up on the *first* merge; prove it stays fixed."""
    script = roundcube_script

    for index in range(3):
        script = merge_simple(script, f"added-{index}", f"INBOX.Add{index}")

    assert rule_names(parse_script(script)) == [
        "keep-boss",
        "bin-the-noise",
        "added-0",
        "added-1",
        "added-2",
    ]


# ############################################################################
# Parse failure is a hard stop
# ############################################################################

UNPARSEABLE = [
    pytest.param("this is not sieve at all ;;;", id="not-sieve"),
    pytest.param(
        'if header :contains "from" "x" {\n    fileinto "A";\n',
        id="unterminated-block",
    ),
    pytest.param(
        'if header :contains "from" "x" {\n    fileinto "A";\n}\n',
        id="missing-require",
    ),
    pytest.param(
        'if header :contains "from" {\n    stop;\n}\n', id="malformed-test"
    ),
]


# ----------------------------------------------------------------------------
@pytest.mark.parametrize("script", UNPARSEABLE)
def test_merge_refuses_an_unparseable_script(script):
    """ADR 0002's load-bearing clause: never fall back to overwriting.

    The tempting recovery from "sievelib cannot parse this" is "then just
    write ours", which converts a visible error into exactly the silent
    data loss the merge exists to prevent -- and does so precisely when the
    script is unusual, hand-written, and most valuable.
    """
    with pytest.raises(MxFilterError, match="could not be parsed"):
        merge_simple(script, "new-rule", "INBOX.New")


# ----------------------------------------------------------------------------
@pytest.mark.parametrize("script", UNPARSEABLE)
def test_remove_refuses_an_unparseable_script(script):
    """The same hard stop on the removal path, which also re-renders."""
    with pytest.raises(MxFilterError, match="could not be parsed"):
        remove_rule(script, "anything")


# ----------------------------------------------------------------------------
def test_the_parse_failure_names_the_reason():
    """A hard stop the user cannot act on is only half a hard stop.

    The message has to carry sievelib's own diagnostic, or the user is
    told their script is unparseable with no way to find out why.
    """
    with pytest.raises(MxFilterError, match=r"line 1.*unknown command"):
        parse_script("garbage garbage;")


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="empty"),
        pytest.param("   \n\n\t", id="whitespace-only"),
    ],
)
def test_parse_treats_an_absent_script_as_an_empty_set(text):
    """An account with no filters yet is a normal starting state."""
    assert rule_names(parse_script(text)) == []


# ############################################################################
# Merge and replace
# ############################################################################


# ----------------------------------------------------------------------------
def test_merge_into_an_empty_script_yields_one_rule(reparse):
    merged = merge_simple("", "first", "INBOX.First")

    reparse(merged)

    assert rule_names(parse_script(merged)) == ["first"]


# ----------------------------------------------------------------------------
def test_merge_refuses_a_duplicate_name_without_replace():
    """Silently overwriting a rule the user named is a data loss too."""
    existing = merge_simple("", "shared", "INBOX.First")

    with pytest.raises(MxFilterError, match=r"already exists.*--replace"):
        merge_simple(existing, "shared", "INBOX.Second")


# ----------------------------------------------------------------------------
def test_replace_updates_the_named_rule_and_leaves_the_others(reparse):
    existing = merge_simple("", "keeper", "INBOX.Keeper")
    existing = merge_simple(existing, "target", "INBOX.Old")

    merged = merge_simple(existing, "target", "INBOX.New", replace=True)

    reparse(merged)

    assert rule_names(parse_script(merged)) == ["keeper", "target"]
    assert 'fileinto "INBOX.Keeper";' in merged
    assert 'fileinto "INBOX.New";' in merged
    assert 'fileinto "INBOX.Old";' not in merged


# ############################################################################
# Removal
# ############################################################################


# ----------------------------------------------------------------------------
def test_remove_takes_out_only_the_named_rule(reparse):
    """The survivors must be identical in meaning, not merely present.

    Comparing against the same script rendered from scratch with only the
    surviving rule is a stronger check than substring assertions: it
    catches a changed comparator, a dropped action, or a mangled require
    line, all of which would leave the substrings intact.
    """
    existing = merge_simple("", "doomed", "INBOX.Doomed")
    existing = merge_simple(existing, "survivor", "INBOX.Survivor")

    after = remove_rule(existing, "doomed")
    expected = merge_simple("", "survivor", "INBOX.Survivor")

    reparse(after)

    assert after == expected


# ----------------------------------------------------------------------------
def test_remove_the_last_rule_leaves_a_parseable_script():
    existing = merge_simple("", "only", "INBOX.Only")

    after = remove_rule(existing, "only")

    assert rule_names(parse_script(after)) == []


# ----------------------------------------------------------------------------
def test_remove_an_unknown_rule_raises_and_lists_the_real_names():
    """The failure has to be actionable, since rule names are discovered."""
    existing = merge_simple("", "real-one", "INBOX.Real")

    with pytest.raises(MxFilterError, match=r"no rule named 'ghost'"):
        remove_rule(existing, "ghost")

    with pytest.raises(MxFilterError, match=r"Known rules: real-one"):
        remove_rule(existing, "ghost")


# ----------------------------------------------------------------------------
def test_remove_from_an_empty_script_says_none_are_known():
    with pytest.raises(MxFilterError, match=r"Known rules: \(none\)"):
        remove_rule("", "ghost")


# ############################################################################
# String escaping
# ############################################################################


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        pytest.param("\\Seen", "\\\\Seen", id="imap-flag"),
        pytest.param('say "hi"', 'say \\"hi\\"', id="double-quote"),
        pytest.param("C:\\path", "C:\\\\path", id="backslash-mid-value"),
        pytest.param("plain", "plain", id="nothing-to-escape"),
    ],
)
def test_escape_sieve_string(raw, escaped):
    assert escape_sieve_string(raw) == escaped


# ----------------------------------------------------------------------------
def test_a_flag_survives_the_round_trip_as_two_backslashes(reparse):
    """``--flag '\\Seen'`` must reach the server as the flag the user typed.

    RFC 5228 quoted strings escape backslash, and sievelib quotes without
    escaping -- so an unescaped value is emitted as ``"\\Seen"``, which the
    server reads back as the flag ``Seen``. That flag does not exist, so
    the rule quietly does nothing.
    """
    criteria = Criteria()
    criteria.add("subject", "anything")

    merged = merge_rule(
        "",
        "flagger",
        criteria.sieve_conditions(),
        [("addflag", escape_sieve_string("\\Seen")), ("stop",)],
        criteria.sieve_matchtype(),
    )

    assert 'addflag "\\\\Seen";' in merged

    reparse(merged)


# ----------------------------------------------------------------------------
def test_a_quoted_value_survives_the_round_trip(reparse):
    criteria = Criteria()
    criteria.add("subject", 'the "quoted" one')

    merged = merge_rule(
        "",
        "quoted",
        criteria.sieve_conditions(),
        [("stop",)],
        criteria.sieve_matchtype(),
    )

    assert 'the \\"quoted\\" one' in merged

    reparse(merged)


# ############################################################################
# Diff, naming, backups
# ############################################################################


# ----------------------------------------------------------------------------
def test_script_diff_is_empty_when_nothing_changed():
    """The dry-run diff is the user-facing proof that a merge merged.

    An empty diff for an unchanged script is what lets the CLI say "no
    change" instead of showing a confusing zero-line patch.
    """
    script = merge_simple("", "one", "INBOX.One")

    assert script_diff(script, script) == ""


# ----------------------------------------------------------------------------
def test_script_diff_shows_the_added_rule():
    before = merge_simple("", "one", "INBOX.One")
    after = merge_simple(before, "two", "INBOX.Two")

    diff = script_diff(before, after, name="active")

    assert "active (current)" in diff
    assert "active (proposed)" in diff
    assert "+# rule:[two]" in diff


# ----------------------------------------------------------------------------
def test_rule_names_reports_the_rules_in_order():
    script = merge_simple("", "first", "INBOX.A")
    script = merge_simple(script, "second", "INBOX.B")

    assert rule_names(parse_script(script)) == ["first", "second"]


# ----------------------------------------------------------------------------
def test_render_script_round_trips_through_parse():
    script = merge_simple("", "one", "INBOX.One")

    assert render_script(parse_script(script)) == script


# ----------------------------------------------------------------------------
def test_backup_writes_the_exact_bytes_the_server_had(tmp_path):
    """Restoring must not need mxfilter, so the file is a plain copy."""
    text = 'require ["fileinto"];\n# untouched\n'

    target = backup_script(text, "roundcube", tmp_path / "backups")

    assert target.parent == tmp_path / "backups"
    assert target.name.startswith("roundcube-")
    assert target.suffix == ".sieve"
    assert target.read_text(encoding="utf-8") == text


# ----------------------------------------------------------------------------
def test_backup_sanitizes_a_script_name_with_path_separators(tmp_path):
    """A server-supplied script name must not be able to escape the dir."""
    target = backup_script("x", "../../etc/passwd", tmp_path / "backups")

    assert target.parent == tmp_path / "backups"
    assert "/" not in target.name


# ----------------------------------------------------------------------------
def test_backup_failure_raises_rather_than_losing_the_upload_guard(tmp_path):
    """A backup that cannot be written must stop the upload, not proceed.

    The backup is the only thing standing between a bad merge and an
    unrecoverable script, so its failure has to be fatal and named.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")

    with pytest.raises(MxFilterError, match="could not write backup"):
        backup_script("x", "active", blocker / "backups")

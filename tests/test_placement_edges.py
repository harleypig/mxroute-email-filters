"""Adversarial tests for placement: hunting a lost rule and an off-by-one.

``test_placement.py`` and ``test_sieve.py`` prove the placement flags do what
they say on the shapes they were written for. This file assumes they do not.

Two properties are worth attacking, and only two:

* **ADR 0002 -- rules mxfilter did not write must survive.** Reordering is a
  brand-new way to break that. Every earlier merge appended, so the list the
  user's hand-made rules live in was never rewritten; now it is, on every
  placed rule. A reorder that drops a rule, mangles a body, loses a name, or
  strips an extension from the ``require`` line destroys work Roundcube's
  filter UI put there -- silently, because PUTSCRIPT succeeds either way.
* **The index arithmetic.** ``resolve_position`` returns an index into the
  script *with the target name removed*, which is what makes ``--replace``
  coherent and is exactly where an off-by-one hides. A wrong index does not
  raise: it files the rule one slot from where the user asked, which renders
  perfectly and sorts mail differently.

The tests fall into two kinds, both deliberate. **Boundaries that hold** pass,
and earn their line count because "we tried this and it was fine" is a
different statement from "nobody has tried this". **Over-claims** are marked
``xfail(strict=True)`` and assert the answer the code *should* give, so a fix
turns them green rather than leaving them to be rewritten.
"""

import re

import pytest

from mxfilter import MxFilterError
from mxfilter.cli import build_parser, warn_about_placement
from mxfilter.criteria import Criteria
from mxfilter.rules import analyze_placement, read_rules, rule_from_criteria
from mxfilter.sieve import (
    PLACE_AFTER,
    PLACE_BEFORE,
    PLACE_FIRST,
    PLACE_LAST,
    Placement,
    merge_rule,
    parse_script,
    render_script,
    resolve_position,
    rule_names,
)

# A script in the state an MXroute account is actually in: written by two
# different tools, in two different name dialects, with four structurally
# unalike rules. Every one of them is something a reorder could damage in a
# way a string assertion on the *new* rule would never notice.
#
#   boss   -- Roundcube marker, one test, two actions plus a flag and stop
#   noise  -- sievelib marker, an anyof of two tests, and NO stop
#   urgent -- Roundcube marker, an allof, a multi-key list, a second header
#   spam   -- sievelib marker, and stop as its only action
MIXED_SCRIPT = """require ["fileinto","imap4flags"];
# rule:[boss]
if header :contains "from" "boss@example.com"
{
\tfileinto "INBOX.Boss";
\tsetflag "\\\\Flagged";
\tstop;
}
# Filter: noise
if anyof (header :contains "subject" "newsletter", \
header :is "from" "bulk@example.com")
{
\tfileinto "INBOX.Noise";
}
# rule:[urgent]
if allof (header :contains "to" ["a@example.com","b@example.com"], \
header :contains "subject" "urgent")
{
\tfileinto "INBOX.Urgent";
\taddflag "\\\\Seen";
\tstop;
}
# Filter: spam
if header :is "x-spam-flag" "yes"
{
\tstop;
}
"""

MIXED_NAMES = ["boss", "noise", "urgent", "spam"]

# ############################################################################
# Helpers
# ############################################################################


# ----------------------------------------------------------------------------
def merge(script: str, name: str, folder: str, **kwargs) -> str:
    """Merge a plain 'file it here and stop' rule into ``script``."""
    criteria = Criteria()
    criteria.add("from", "somebody@example.com")

    return merge_rule(
        script,
        name,
        criteria.sieve_conditions(),
        [("fileinto", folder), ("stop",)],
        criteria.sieve_matchtype(),
        **kwargs,
    )


# ----------------------------------------------------------------------------
def _criteria(header: str, value: str) -> Criteria:
    """Return a one-term Criteria, for the analysis and CLI tests."""
    criteria = Criteria()
    criteria.add(header, value)

    return criteria


# ----------------------------------------------------------------------------
def script_of(*names: str) -> str:
    """Return a rendered script holding one plain rule per name, in order."""
    script = ""

    for name in names:
        script = merge(script, name, f"INBOX.{name}")

    return script


# ----------------------------------------------------------------------------
def rule_blocks(text: str) -> dict[str, str]:
    """Split a rendered script into its rule bodies, keyed by rule name.

    Everything mxfilter emits carries a Roundcube name marker, so the
    markers are the block boundaries. The bodies come back stripped, which
    is what lets two renders be compared for byte identity without the
    blank line between rules counting as a difference.
    """
    blocks: dict[str, list[str]] = {}
    current = None

    for line in text.splitlines():
        marker = re.fullmatch(r"# rule:\[(?P<name>.+)\]", line.strip())

        if marker is not None:
            current = marker["name"]
            blocks[current] = []

        elif current is not None:
            blocks[current].append(line)

    return {name: "\n".join(body).strip() for name, body in blocks.items()}


# ----------------------------------------------------------------------------
def require_line(text: str) -> str:
    """Return the require line, which every merge regenerates from scratch."""
    return text.splitlines()[0]


# ----------------------------------------------------------------------------
def baseline() -> str:
    """Return MIXED_SCRIPT as mxfilter renders it, with nothing moved.

    A merge normalises whitespace, so the user's original text is not the
    thing a reorder has to preserve -- the *rendered* form is. Comparing a
    reordered render against this one is what isolates the reorder from the
    formatting every merge does anyway.
    """
    return render_script(parse_script(MIXED_SCRIPT))


# ############################################################################
# ADR 0002 -- what a reorder must not damage
# ############################################################################


# ----------------------------------------------------------------------------
def test_both_name_dialects_survive_a_reorder_attached_to_the_right_rules():
    """A reorder must not shuffle names against bodies.

    Names and bodies live in the same dict entry, so nothing *obviously*
    detaches them -- which is the reason to check. mxfilter reads two
    dialects and writes one, so a rule Roundcube named and a rule sievelib
    named travel through the reorder by different routes, and a name that
    lands on its neighbour's body is a rule the user can no longer find by
    the name they gave it, plus a ``--replace`` that silently rewrites the
    wrong filter.
    """
    merged = merge(
        MIXED_SCRIPT, "new", "INBOX.New", placement=Placement(PLACE_FIRST)
    )

    blocks = rule_blocks(merged)

    assert rule_names(parse_script(merged)) == ["new", *MIXED_NAMES]

    # Every name is emitted in Roundcube's dialect whichever it arrived in,
    # so the panel keeps seeing all four rules.
    assert "# Filter: " not in merged

    assert 'setflag "\\\\Flagged";' in blocks["boss"]
    assert 'header :is "from" "bulk@example.com"' in blocks["noise"]
    assert '"a@example.com", "b@example.com"' in blocks["urgent"]
    assert blocks["spam"].endswith("stop;\n}")


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "placement",
    [
        Placement(PLACE_FIRST),
        Placement(PLACE_LAST),
        Placement(PLACE_BEFORE, "boss"),
        Placement(PLACE_BEFORE, "spam"),
        Placement(PLACE_AFTER, "boss"),
        Placement(PLACE_AFTER, "spam"),
    ],
)
def test_inserting_a_new_rule_leaves_every_body_byte_identical(placement):
    """The ADR 0002 floor, restated for the reorder path.

    Not "the conditions are still in there somewhere" -- byte identity of
    each rule's whole body against a render that moved nothing. A dropped
    action, a re-quoted key, a mangled flag backslash, or a lost second
    header would all pass a substring check on the conditions and still
    change how the user's mail is sorted.
    """
    before = rule_blocks(baseline())
    after = rule_blocks(
        merge(MIXED_SCRIPT, "new", "INBOX.New", placement=placement)
    )

    for name in MIXED_NAMES:
        assert after[name] == before[name], name


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("moved", "where", "anchor", "expected"),
    [
        ("boss", PLACE_BEFORE, "noise", ["boss", "noise", "urgent", "spam"]),
        ("boss", PLACE_AFTER, "noise", ["noise", "boss", "urgent", "spam"]),
        ("boss", PLACE_BEFORE, "urgent", ["noise", "boss", "urgent", "spam"]),
        ("boss", PLACE_AFTER, "urgent", ["noise", "urgent", "boss", "spam"]),
        ("boss", PLACE_BEFORE, "spam", ["noise", "urgent", "boss", "spam"]),
        ("boss", PLACE_AFTER, "spam", ["noise", "urgent", "spam", "boss"]),
        ("noise", PLACE_BEFORE, "boss", ["noise", "boss", "urgent", "spam"]),
        ("noise", PLACE_AFTER, "boss", ["boss", "noise", "urgent", "spam"]),
        ("noise", PLACE_BEFORE, "urgent", ["boss", "noise", "urgent", "spam"]),
        ("noise", PLACE_AFTER, "urgent", ["boss", "urgent", "noise", "spam"]),
        ("noise", PLACE_BEFORE, "spam", ["boss", "urgent", "noise", "spam"]),
        ("noise", PLACE_AFTER, "spam", ["boss", "urgent", "spam", "noise"]),
        ("urgent", PLACE_BEFORE, "boss", ["urgent", "boss", "noise", "spam"]),
        ("urgent", PLACE_AFTER, "boss", ["boss", "urgent", "noise", "spam"]),
        ("urgent", PLACE_BEFORE, "noise", ["boss", "urgent", "noise", "spam"]),
        ("urgent", PLACE_AFTER, "noise", ["boss", "noise", "urgent", "spam"]),
        ("urgent", PLACE_BEFORE, "spam", ["boss", "noise", "urgent", "spam"]),
        ("urgent", PLACE_AFTER, "spam", ["boss", "noise", "spam", "urgent"]),
        ("spam", PLACE_BEFORE, "boss", ["spam", "boss", "noise", "urgent"]),
        ("spam", PLACE_AFTER, "boss", ["boss", "spam", "noise", "urgent"]),
        ("spam", PLACE_BEFORE, "noise", ["boss", "spam", "noise", "urgent"]),
        ("spam", PLACE_AFTER, "noise", ["boss", "noise", "spam", "urgent"]),
        ("spam", PLACE_BEFORE, "urgent", ["boss", "noise", "spam", "urgent"]),
        ("spam", PLACE_AFTER, "urgent", ["boss", "noise", "urgent", "spam"]),
    ],
)
def test_moving_any_rule_to_any_position_lands_it_there_and_disturbs_nothing(
    moved, where, anchor, expected, reparse
):
    """Every source position against every target, with the full order pinned.

    Two assertions per case, and they fail for different reasons. The order
    catches an off-by-one in the resolver. The byte identity of the three
    rules that did not move catches the reorder damaging what it stepped
    over -- the ADR 0002 failure, which an order assertion alone would
    happily pass while the bodies rot.

    The cases where the rule is already in the requested slot are included
    on purpose: a resolver that shifted by one would move it out of a
    correct position, which is the least visible way to be wrong.
    """
    before = rule_blocks(baseline())

    merged = merge(
        MIXED_SCRIPT,
        moved,
        "INBOX.Moved",
        replace=True,
        placement=Placement(where, anchor),
    )

    reparse(merged)

    assert rule_names(parse_script(merged)) == expected

    after = rule_blocks(merged)

    for name in MIXED_NAMES:
        if name != moved:
            assert after[name] == before[name], name


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "placement",
    [
        Placement(PLACE_FIRST),
        Placement(PLACE_LAST),
        Placement(PLACE_BEFORE, "urgent"),
        Placement(PLACE_AFTER, "boss"),
    ],
)
def test_a_reorder_neither_drops_nor_duplicates_a_requirement(placement):
    """The require line is rebuilt from the union, so a reorder can lose it.

    ``imap4flags`` is required by two rules here and ``fileinto`` by three,
    so a reorder that rebuilt the union from a truncated list would still
    emit a plausible-looking require line -- and a script requiring less
    than it uses is one the server rejects at PUTSCRIPT, or worse, accepts
    and then fails on. A duplicated entry is the same bug from the other
    side.
    """
    merged = merge(MIXED_SCRIPT, "new", "INBOX.New", placement=placement)

    assert require_line(merged) == require_line(baseline())


# ----------------------------------------------------------------------------
def test_a_reordered_script_re_renders_to_itself(reparse):
    """Parse, render, parse, render -- the second pass must change nothing.

    The next ``mxfilter add`` parses whatever this one wrote, so instability
    here would mean a script that drifts on every run, and a diff that shows
    changes nobody asked for. The reorder is the new thing that could
    introduce it, because it is the only step that rewrites the rule list.
    """
    once = merge(
        MIXED_SCRIPT,
        "urgent",
        "INBOX.Moved",
        replace=True,
        placement=Placement(PLACE_FIRST),
    )

    reparse(once)

    twice = render_script(parse_script(once))

    assert twice == once
    assert render_script(parse_script(twice)) == once


# A rule whose *body* contains text shaped exactly like a name marker, both
# as a match key and as a folder name. Nothing in the script has those
# names; a reorder that reasoned about rules by scanning the rendered text
# would think otherwise.
DECOY_SCRIPT = """require ["fileinto"];
# rule:[holder]
if header :contains "subject" "# rule:[phantom]"
{
\tfileinto "INBOX.# rule:[fake]";
\tstop;
}
# rule:[second]
if header :contains "to" "second@example.com"
{
\tfileinto "INBOX.Second";
\tstop;
}
"""


# ----------------------------------------------------------------------------
def test_a_name_marker_inside_a_string_is_not_a_rule_to_reorder_around():
    """Rule identity comes from the parser, never from reading the text.

    Both halves matter. The decoy must survive the reorder byte for byte --
    a rewrite that spliced on a text match would edit the user's folder name
    -- and the decoy must not become an anchor, because a resolver that
    found ``phantom`` would place the rule at an offset no rule occupies.
    """
    merged = merge(
        DECOY_SCRIPT, "new", "INBOX.New", placement=Placement(PLACE_FIRST)
    )

    assert rule_names(parse_script(merged)) == ["new", "holder", "second"]
    assert 'header :contains "subject" "# rule:[phantom]"' in merged
    assert 'fileinto "INBOX.# rule:[fake]";' in merged

    with pytest.raises(MxFilterError, match="no rule named 'phantom'"):
        merge(
            DECOY_SCRIPT,
            "new",
            "INBOX.New",
            placement=Placement(PLACE_BEFORE, "phantom"),
        )


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "placement",
    [None, Placement(PLACE_FIRST), Placement(PLACE_BEFORE, "anything")],
)
def test_a_placement_flag_opens_no_way_around_the_parse_hard_stop(placement):
    """ADR 0002's load-bearing clause, checked against the newest caller.

    A script sievelib cannot read must fail, never fall back to writing
    ours -- that fallback turns a visible error into the exact data loss the
    ADR exists to prevent, and it is most tempting precisely where it is
    most costly: an unusual hand-written script is both the hardest to parse
    and the most valuable to keep.

    ``--before`` on a script with no readable rules is the shape that could
    plausibly acquire its own "well, put it first then" branch, so it is
    included alongside the plain cases.
    """
    unparseable = 'require ["fileinto"];\nif header :contains "to" {\n oops\n'

    with pytest.raises(MxFilterError, match="could not be parsed"):
        merge(unparseable, "new", "INBOX.New", placement=placement)


# ----------------------------------------------------------------------------
def test_a_name_collision_is_reported_before_a_placement_is_resolved():
    """The error has to name the thing the user must fix first.

    Adding a rule whose name is already taken needs ``--replace`` whichever
    placement was asked for, so reporting a placement problem here would
    send the user off to check their anchor for a fault that is not there --
    and the anchor may be perfectly valid, as it is below.
    """
    with pytest.raises(MxFilterError, match="already exists"):
        merge(
            script_of("taken", "other"),
            "taken",
            "INBOX.New",
            placement=Placement(PLACE_BEFORE, "other"),
        )


# ############################################################################
# The index arithmetic
# ############################################################################


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("moved", "where", "anchor", "expected"),
    [
        # The classic off-by-one pair: an anchor sitting immediately either
        # side of the rule being moved. Both are no-ops when the arithmetic
        # is right, and both move the rule one slot when it is not.
        ("c", PLACE_BEFORE, "d", ["a", "b", "c", "d", "e"]),
        ("c", PLACE_AFTER, "b", ["a", "b", "c", "d", "e"]),
        # And the same two anchors read the other way round, which must
        # move it.
        ("c", PLACE_BEFORE, "b", ["a", "c", "b", "d", "e"]),
        ("c", PLACE_AFTER, "d", ["a", "b", "d", "c", "e"]),
    ],
)
def test_an_anchor_beside_the_rule_being_moved(moved, where, anchor, expected):
    """Where an index measured against the wrong list shows up first.

    ``resolve_position`` counts the *other* rules, so an anchor behind the
    moved rule has already slid forward by one when it is looked up. Get
    that wrong and "put c before d" -- where c is already immediately
    before d -- moves c anyway, which is a diff the user did not ask for and
    a different filing for any message both rules match.
    """
    merged = merge(
        script_of("a", "b", "c", "d", "e"),
        moved,
        "INBOX.Moved",
        replace=True,
        placement=Placement(where, anchor),
    )

    assert rule_names(parse_script(merged)) == expected


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("moved", "where", "anchor", "expected"),
    [
        ("e", PLACE_BEFORE, "a", ["e", "a", "b", "c", "d"]),
        ("e", PLACE_AFTER, "a", ["a", "e", "b", "c", "d"]),
        ("a", PLACE_BEFORE, "e", ["b", "c", "d", "a", "e"]),
        ("a", PLACE_AFTER, "e", ["b", "c", "d", "e", "a"]),
    ],
)
def test_anchoring_on_the_first_and_last_rule(moved, where, anchor, expected):
    """The two ends, where a bad index runs off the list instead of shifting.

    ``insert`` clamps rather than raising, so an index one past the end is
    not an error -- it is a rule quietly filed last. Pinning both ends is
    what makes that visible, and the full-length move (front to back and
    back to front) is the longest distance the arithmetic has to survive.
    """
    merged = merge(
        script_of("a", "b", "c", "d", "e"),
        moved,
        "INBOX.Moved",
        replace=True,
        placement=Placement(where, anchor),
    )

    assert rule_names(parse_script(merged)) == expected


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("placement", "expected"),
    [
        (None, ["a", "b", "c"]),
        (Placement(PLACE_FIRST), ["b", "a", "c"]),
        (Placement(PLACE_LAST), ["a", "c", "b"]),
    ],
)
def test_replace_moves_a_rule_only_when_asked(placement, expected):
    """Three flags, three distinct answers, on one middle rule.

    The middle of the script is the only place the three can be told apart:
    anchored at either end, "leave it alone" coincides with one of the two
    explicit flags and a resolver that confused them would still look
    right. An implementation that read a missing placement as ``--last``
    would demote a rule on an ordinary ``--replace`` that said nothing
    about position at all.
    """
    merged = merge(
        script_of("a", "b", "c"),
        "b",
        "INBOX.Moved",
        replace=True,
        placement=placement,
    )

    assert rule_names(parse_script(merged)) == expected


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "placement",
    [None, Placement(PLACE_FIRST), Placement(PLACE_LAST)],
)
def test_a_one_rule_script_survives_being_replaced_in_place(placement):
    """The degenerate script, where every index is the same index.

    With one rule the "other rules" list is empty, so ``--first`` and
    ``--last`` both resolve to 0 and the pop-then-insert has nothing to
    measure against. It is the smallest input that can expose a resolver
    reaching past the end, and it is what the very first account looks like.
    """
    merged = merge(
        script_of("only"),
        "only",
        "INBOX.Changed",
        replace=True,
        placement=placement,
    )

    assert rule_names(parse_script(merged)) == ["only"]
    assert 'fileinto "INBOX.Changed";' in merged


# ----------------------------------------------------------------------------
def test_resolve_position_never_answers_with_a_negative_index():
    """A negative index is not an error here -- it is the front of the script.

    ``analyze_placement`` clamps with ``max(0, at_index)`` and ``list.insert``
    counts backwards, so a ``-1`` escaping the resolver would not raise: it
    would place the rule at, or one short of, the opposite end from the one
    the user asked for. Sweeping every flag against every name -- including
    a name that is not in the script -- is cheap insurance against exactly
    that.
    """
    names = ["a", "b", "c", "d", "e"]

    placements = [None, Placement(PLACE_FIRST), Placement(PLACE_LAST)]
    placements += [
        Placement(where, anchor)
        for where in (PLACE_BEFORE, PLACE_AFTER)
        for anchor in names
    ]

    for name in [*names, "absent"]:
        for placement in placements:
            if placement is not None and placement.anchor == name:
                continue

            answer = resolve_position(names, placement, name)

            assert 0 <= answer <= len(names), (name, placement)


# ----------------------------------------------------------------------------
def test_an_anchor_that_is_a_substring_of_another_name_matches_only_itself():
    """Names are compared whole, not searched for.

    ``Lists`` sits inside ``Lists-GitHub``, and a resolver that reached for
    a prefix match -- or that scanned the rendered text rather than the name
    list -- would anchor on whichever came first and file the rule against
    the wrong neighbour. The two rules are adjacent here so that the wrong
    answer is one slot away, not obviously absurd.
    """
    script = script_of("Lists", "Lists-GitHub", "Other")

    before = merge(
        script, "new", "INBOX.New", placement=Placement(PLACE_BEFORE, "Lists")
    )
    after = merge(
        script,
        "new",
        "INBOX.New",
        placement=Placement(PLACE_AFTER, "Lists"),
    )

    assert rule_names(parse_script(before))[:2] == ["new", "Lists"]
    assert rule_names(parse_script(after))[:2] == ["Lists", "new"]


# ----------------------------------------------------------------------------
@pytest.mark.parametrize("anchor", ["lists", "LISTS", "Lists "])
def test_an_anchor_that_only_looks_like_a_rule_name_is_refused(anchor):
    """Exact match, with no case folding and no whitespace forgiveness.

    A near-miss anchor is a typo, and the two ways to be wrong here are
    opposite: matching it loosely files the rule against a rule the user did
    not name, while appending instead would report success and change
    nothing. Refusing, and listing the names that do exist, is the only
    answer that tells them what happened.

    The trailing-space case is the sharp one. ``# rule:[Lists ]`` parses as
    ``Lists`` -- the space is not part of the name -- so a user copying the
    marker text verbatim is holding a string the script does not contain.
    """
    with pytest.raises(MxFilterError, match="no rule named"):
        merge(
            script_of("Lists", "Other"),
            "new",
            "INBOX.New",
            placement=Placement(PLACE_BEFORE, anchor),
        )


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name", ["Läufer", "日本語", "a]b", "with space", "sale: 50%"]
)
def test_a_rule_name_that_is_not_plain_ascii_works_as_an_anchor(name):
    """Rule names are user text, and users do not write ASCII identifiers.

    The name markers are rewritten by splicing utf-8 *bytes* at lexer
    offsets, so a multi-byte name is the case where a character-offset bug
    would corrupt the script rather than merely mis-name a rule -- and
    ``a]b`` is the one that tests the marker pattern's own greediness,
    since the closing bracket appears twice on the line.
    """
    script = script_of(name, "other")

    assert rule_names(parse_script(script)) == [name, "other"]

    merged = merge(
        script, "new", "INBOX.New", placement=Placement(PLACE_AFTER, name)
    )

    assert rule_names(parse_script(merged)) == [name, "new", "other"]


# ############################################################################
# The -1 foot-gun
# ############################################################################


# One broad stopping rule, so the analysis has an unambiguous verdict in
# each direction: reached before the candidate it kills it outright, reached
# after it, it is what the candidate would starve.
BROAD_SCRIPT = """require ["fileinto"];
# rule:[Lists]
if header :contains "to" "@lists.example.com"
{
\tfileinto "INBOX.Lists";
\tstop;
}
"""


# ----------------------------------------------------------------------------
def test_minus_one_and_none_mean_opposite_ends_to_the_analysis():
    """Two defaults, one value apart, pointing in opposite directions.

    ``analyze_placement`` clamps with ``max(0, at_index)``, so ``-1`` is the
    **front** of the script, while ``at_index=None`` is the **end**. And
    ``rule_from_criteria`` defaults its own ``index`` to ``-1``. Nothing
    joins those two today -- the candidate's ``index`` is never read as a
    position -- but they sit one attribute access apart, and the wrong one
    would not raise: it would report on the opposite end of the script.

    This pins the trap open so a later refactor that passes the candidate's
    index through as ``at_index`` fails here instead of in somebody's inbox.
    """
    existing = read_rules(parse_script(BROAD_SCRIPT))
    candidate = rule_from_criteria(
        "New",
        _criteria("to", "announce@lists.example.com"),
        ("fileinto", "stop"),
        stops=True,
    )

    assert candidate.index == -1

    front = analyze_placement(existing, candidate, at_index=-1)
    end = analyze_placement(existing, candidate, at_index=None)

    # At the front nothing precedes the rule, so nothing can shadow it, and
    # the one existing rule is behind it. At the end it is the other way
    # round, and 'Lists' provably covers the candidate.
    assert not front.dead_on_arrival
    assert front.starves

    assert end.dead_on_arrival
    assert not end.starves


# ############################################################################
# The CLI join -- does the resolved index actually reach the analysis
# ############################################################################
#
# The resolver can be perfect and the warning still describe an append. That
# failure is invisible: `--first` starves three working rules, the command
# reports nothing, and the user finds out when the mail stops arriving --
# which is the exact silent success the placement work exists to remove.

# Two rules that a broad `@lists.example.com` rule provably covers, so the
# analysis has something CERTAIN to say in whichever direction it looks.
NARROW_SCRIPT = """require ["fileinto"];
# rule:[Announce]
if header :contains "to" "announce@lists.example.com"
{
\tfileinto "INBOX.Announce";
\tstop;
}
# rule:[Digest]
if header :contains "to" "digest@lists.example.com"
{
\tfileinto "INBOX.Digest";
\tstop;
}
"""

BROAD_ACTIONS = [("fileinto", "INBOX.Lists"), ("stop",)]


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("placement", "starved"),
    [
        (Placement(PLACE_FIRST), ["Announce", "Digest"]),
        (Placement(PLACE_BEFORE, "Digest"), ["Digest"]),
        (Placement(PLACE_AFTER, "Announce"), ["Digest"]),
        (Placement(PLACE_LAST), []),
        (None, []),
    ],
)
def test_the_warning_describes_the_position_the_rule_will_occupy(
    placement, starved, capsys
):
    """Same rule, same script, five different verdicts -- decided by the flag.

    This is the join under test: the flag has to reach ``resolve_position``
    and its answer has to reach ``analyze_placement``. Break either link and
    every row here collapses to the ``None`` row, which reports nothing at
    all for the two cases that starve working rules.
    """
    warn_about_placement(
        NARROW_SCRIPT,
        "Lists",
        _criteria("to", "@lists.example.com"),
        BROAD_ACTIONS,
        placement,
    )

    output = capsys.readouterr().out

    for name in ("Announce", "Digest"):
        expected = name in starved
        found = f"{name!r} never runs" in output

        assert found is expected, (name, output)


# ----------------------------------------------------------------------------
def test_replacing_a_rule_does_not_report_it_shadowing_itself(capsys):
    """The old copy is on its way out, so it is not part of the comparison.

    Leaving it in would have every ``--replace`` report the rule as dead on
    arrival behind its own previous version -- a warning that is always
    wrong, always present, and therefore trains the user to ignore the
    warnings that are not. It would also put the index out by one, since
    ``resolve_position`` counts the other rules only.
    """
    warn_about_placement(
        NARROW_SCRIPT,
        "Announce",
        _criteria("to", "@lists.example.com"),
        BROAD_ACTIONS,
        Placement(PLACE_FIRST),
    )

    output = capsys.readouterr().out

    assert "'Announce' never runs" not in output
    assert "'Digest' never runs" in output


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "argv",
    [
        ["--first", "--last"],
        ["--first", "--before", "X"],
        ["--first", "--after", "X"],
        ["--last", "--before", "X"],
        ["--last", "--after", "X"],
        ["--before", "X", "--after", "Y"],
    ],
)
def test_no_two_placement_flags_are_accepted_together(argv):
    """All six pairs, not just the one.

    ``placement_from_args`` reads the four flags in a fixed order, so a pair
    that slipped past argparse would not error -- the first branch would
    win and the other flag would be dropped in silence. The exclusion is
    what makes that unreachable, and it is one keyword away from being lost
    when a fifth flag is added.
    """
    with pytest.raises(SystemExit):
        build_parser().parse_args(["add", "--to", "x", *argv])


# ############################################################################
# Duplicate names -- a script the tool did not write, and cannot assume about
# ############################################################################
#
# `# rule:[Lists]` and `# rule:[Lists ]` both parse to `Lists`, so a
# hand-edited script can hold two rules mxfilter sees as one name. That is
# already ambiguous for `--replace` -- it updates one of them -- but the
# reorder is new, and losing one of the two would be an ADR 0002 failure
# rather than an ambiguity.

DUPLICATE_SCRIPT = """require ["fileinto"];
# rule:[Lists]
if header :contains "to" "one@example.com"
{
\tfileinto "INBOX.One";
\tstop;
}
# rule:[Middle]
if header :contains "to" "mid@example.com"
{
\tfileinto "INBOX.Mid";
\tstop;
}
# rule:[Lists ]
if header :contains "to" "two@example.com"
{
\tfileinto "INBOX.Two";
\tstop;
}
"""


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "placement",
    [
        None,
        Placement(PLACE_FIRST),
        Placement(PLACE_LAST),
        Placement(PLACE_BEFORE, "Middle"),
        Placement(PLACE_AFTER, "Middle"),
    ],
)
def test_a_duplicate_name_never_costs_the_user_a_rule(placement, reparse):
    """Whatever the reorder does about position, it must not lose a filter.

    ``resolve_position`` and ``_move_rule`` have to agree on which entry
    leaves the list. They once did not, and a rule landing in the wrong
    place was the visible half of that disagreement; this is the floor
    underneath it, and deliberately a separate assertion from *where* the
    rule lands. Three rules in, three rules out, all three bodies still
    there. Getting the position wrong is a bug. Dropping somebody's
    hand-written rule is ADR 0002.
    """
    assert rule_names(parse_script(DUPLICATE_SCRIPT)) == [
        "Lists",
        "Middle",
        "Lists",
    ]

    merged = merge(
        DUPLICATE_SCRIPT,
        "Lists",
        "INBOX.Changed",
        replace=True,
        placement=placement,
    )

    reparse(merged)

    assert len(rule_names(parse_script(merged))) == 3
    assert 'fileinto "INBOX.Changed";' in merged
    assert 'fileinto "INBOX.Mid";' in merged
    assert 'fileinto "INBOX.Two";' in merged


# ----------------------------------------------------------------------------
def test_no_flag_leaves_a_duplicated_name_exactly_where_it_was():
    """The default path must be indifferent to the script being odd.

    A ``--replace`` with no placement flag promises to change a rule's body
    and nothing else. A duplicated name is the case where the "where it
    already is" lookup could pick the wrong copy's index and shuffle the
    script as a side effect of an edit that said nothing about order.
    """
    merged = merge(DUPLICATE_SCRIPT, "Lists", "INBOX.Changed", replace=True)

    assert 'fileinto "INBOX.Changed";' in merged
    assert merged.index("INBOX.Changed") < merged.index("INBOX.Mid")
    assert merged.index("INBOX.Mid") < merged.index("INBOX.Two")


# ----------------------------------------------------------------------------
def test_last_puts_the_rule_last_even_when_its_name_is_duplicated():
    """``--last`` says "after every existing rule", and here it is not.

    Duplicate names make ``--replace`` ambiguous about *which* copy it
    edits, and that ambiguity is fine -- either answer is defensible. Where
    the rule ends up is not ambiguous: whichever copy was rewritten,
    ``--last`` promised it would be last, and it comes out second of three.

    It matters because these rules carry ``stop``. A user demoting a broad
    rule so a narrow one can run puts it behind one of the two and in front
    of the other, gets a success message, and the narrow rule they were
    rescuing still never fires.
    """
    merged = merge(
        DUPLICATE_SCRIPT,
        "Lists",
        "INBOX.Changed",
        replace=True,
        placement=Placement(PLACE_LAST),
    )

    assert merged.index("INBOX.Changed") > merged.index("INBOX.Two")

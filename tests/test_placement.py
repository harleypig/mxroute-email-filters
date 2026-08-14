"""The placement resolver, and what the analysis makes of its answer.

``test_sieve.py`` proves a placed rule lands where it was told to. This file
covers the two things that are not visible in the rendered script:

* **The index arithmetic.** ``resolve_position`` returns an index into the
  *other* rules -- the script without the rule being placed -- which is the
  only way a ``--replace`` can be both "take the old copy out" and "put the
  new one at position N" without the two answers disagreeing by one.
* **What the index is then used for.** ``analyze_placement`` takes an
  ``at_index`` and decides what the new rule would shadow and be shadowed
  by. Passing it the wrong index does not fail -- it reports on a position
  the rule will not occupy, which is worse than reporting nothing, and the
  ``--first`` case is exactly where it matters (see the last section).
"""

import pytest

from mxfilter import MxFilterError
from mxfilter.cli import build_parser, placement_from_args
from mxfilter.criteria import Criteria
from mxfilter.rules import (
    CERTAIN,
    analyze_placement,
    read_rules,
    rule_from_criteria,
)
from mxfilter.sieve import (
    PLACE_AFTER,
    PLACE_BEFORE,
    PLACE_FIRST,
    PLACE_LAST,
    Placement,
    parse_script,
    resolve_position,
)

NAMES = ["one", "two", "three"]

# ############################################################################
# Helpers
# ############################################################################


# ----------------------------------------------------------------------------
def build(*bodies: str) -> list:
    """Parse a script assembled from rule bodies and reduce it to Rules."""
    body = "\n".join(bodies)

    return read_rules(parse_script(f'require ["fileinto"];\n{body}'))


# ----------------------------------------------------------------------------
def rule(name: str, test: str) -> str:
    """Return one Sieve rule, with a name marker and a ``stop``."""
    return (
        f"# rule:[{name}]\nif {test}\n{{\n"
        f'\tfileinto "INBOX.{name}";\n\tstop;\n}}'
    )


# ----------------------------------------------------------------------------
def candidate(value: str, name: str = "New"):
    """Return a Rule for a stopping rule that is not in the script yet."""
    criteria = Criteria()
    criteria.add("to", value)

    return rule_from_criteria(name, criteria, ("fileinto", "stop"), stops=True)


# ############################################################################
# Resolving a position
# ############################################################################


# ----------------------------------------------------------------------------
def test_no_placement_appends_a_rule_that_is_not_there_yet():
    """None means "whatever mxfilter did before the flags existed"."""
    assert resolve_position(NAMES, None, "new") == 3


# ----------------------------------------------------------------------------
def test_no_placement_keeps_an_existing_rule_at_its_own_index():
    """The ``--replace``-with-no-flag case, and the reason None is not 'end'.

    A replacement that quietly moved to the end would change which of two
    overlapping rules fires first, so the resolver has to answer "where it
    already is" rather than "append" whenever the name is already there.
    """
    assert resolve_position(NAMES, None, "two") == 1


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("placement", "expected"),
    [
        (Placement(PLACE_FIRST), 0),
        (Placement(PLACE_LAST), 3),
        (Placement(PLACE_BEFORE, "one"), 0),
        (Placement(PLACE_BEFORE, "three"), 2),
        (Placement(PLACE_AFTER, "one"), 1),
        (Placement(PLACE_AFTER, "three"), 3),
    ],
)
def test_each_flag_resolves_to_the_index_it_names(placement, expected):
    """The whole resolver for a rule that is not in the script yet."""
    assert resolve_position(NAMES, placement, "new") == expected


# ----------------------------------------------------------------------------
def test_an_anchor_after_the_rule_being_replaced_shifts_down_by_one():
    """The index counts the *other* rules, and this is where that shows.

    Moving 'one' to sit before 'three' means index 1, not 2: 'one' is
    leaving its own position, so everything behind it slides forward. An
    index measured against the unmodified list would land the rule one slot
    too late -- behind 'three' instead of in front of it -- which is the
    quiet kind of wrong that renders fine and files mail differently.
    """
    assert (
        resolve_position(NAMES, Placement(PLACE_BEFORE, "three"), "one") == 1
    )


# ----------------------------------------------------------------------------
def test_an_anchor_before_the_rule_being_replaced_is_unaffected():
    """The other half of the same arithmetic, as a control.

    Rules ahead of the one being moved do not shift, so their indexes are
    the same either way. Pinned so the test above reads as arithmetic
    rather than as an off-by-one someone compensated for.
    """
    assert resolve_position(NAMES, Placement(PLACE_AFTER, "one"), "three") == 1


# ----------------------------------------------------------------------------
def test_last_on_an_empty_script_is_the_same_position_as_first():
    """With no rules there is one position, and every flag resolves to it.

    An empty script is the ordinary first-run state rather than an error
    state, so a habitual ``--first`` must not fail on it.
    """
    assert resolve_position([], Placement(PLACE_FIRST), "new") == 0
    assert resolve_position([], Placement(PLACE_LAST), "new") == 0
    assert resolve_position([], None, "new") == 0


# ----------------------------------------------------------------------------
def test_an_anchor_on_an_empty_script_says_the_script_is_empty():
    """``--before`` still needs something to be before, even here.

    Resolving it to 0 would be guessing at what the user meant. The list of
    known rules reads ``(none)``, which answers the question the error
    raises rather than leaving the user to run ``list`` to find out.
    """
    with pytest.raises(MxFilterError, match=r"Known rules: \(none\)"):
        resolve_position([], Placement(PLACE_BEFORE, "anything"), "new")


# ----------------------------------------------------------------------------
def test_an_unknown_anchor_names_the_flag_that_could_not_be_satisfied():
    """One error has to serve four flags, so it says which one was used.

    ``--before`` and ``--after`` fail identically otherwise, and a user who
    typed one of them wants to be told about that one.
    """
    with pytest.raises(MxFilterError) as raised:
        resolve_position(NAMES, Placement(PLACE_AFTER, "typo"), "new")

    assert "--after" in str(raised.value)
    assert "one, two, three" in str(raised.value)


# ----------------------------------------------------------------------------
def test_naming_the_rule_itself_is_refused_before_the_lookup():
    """Order matters here: self-reference is its own diagnosis.

    A rule that names itself is also absent from the "other rules" list, so
    the unknown-anchor branch would happily fire and tell the user their
    rule does not exist -- while listing it among the known ones. Checking
    self-reference first is what keeps the message honest.
    """
    with pytest.raises(MxFilterError, match="names the rule being added"):
        resolve_position(NAMES, Placement(PLACE_BEFORE, "two"), "two")


# ############################################################################
# From the command line
# ############################################################################
#
# Flag parsing is CLI work, not core logic, and it sits here rather than in
# a CLI test file because it is the one join that nothing else covers: the
# resolver above can be perfect and still never be reached. The failure it
# guards against -- a flag accepted and then ignored -- is the same silent
# success the placement flags were added to remove.


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], None),
        (["--first"], Placement(PLACE_FIRST)),
        (["--last"], Placement(PLACE_LAST)),
        (["--before", "Lists"], Placement(PLACE_BEFORE, "Lists")),
        (["--after", "Lists"], Placement(PLACE_AFTER, "Lists")),
    ],
)
def test_each_flag_reaches_the_resolver_as_a_placement(argv, expected):
    """Every flag maps to exactly one placement, and none maps to none."""
    args = build_parser().parse_args(["add", "--to", "x", *argv])

    assert placement_from_args(args) == expected


# ----------------------------------------------------------------------------
def test_from_message_carries_the_same_flags_as_add():
    """Both subcommands run the same body, so both need the same flags.

    They are attached in one shared helper for that reason; this is the
    test that notices if a later flag is added to only one of them.
    """
    args = build_parser().parse_args(
        ["from-message", "--uid", "1", "--before", "Lists"]
    )

    assert placement_from_args(args) == Placement(PLACE_BEFORE, "Lists")


# ----------------------------------------------------------------------------
def test_two_placement_flags_at_once_are_refused_by_the_parser():
    """Four answers to one question, so argparse rejects the pair.

    Worth pinning because the alternative is not an error but a silent
    precedence order -- whichever branch of ``placement_from_args`` happens
    to be read first would win, and the other flag would be ignored.
    """
    with pytest.raises(SystemExit):
        build_parser().parse_args(["add", "--to", "x", "--first", "--last"])


# ############################################################################
# What the analysis does with it
# ############################################################################


# ----------------------------------------------------------------------------
def test_inserting_first_is_reported_as_starving_the_rule_it_covers():
    """The finding that only exists because placement exists.

    A broad stopping rule put in front of a narrow one takes the narrow
    one's mail and ends evaluation, so the narrow rule stops firing -- and
    it is a *working* rule, which makes this the more expensive of the two
    directions. CERTAIN, because ``@lists.example.com`` provably contains
    every address ``announce@lists.example.com`` matches.
    """
    existing = build(
        rule("Announce", 'header :contains "to" "announce@lists.example.com"')
    )

    at_index = resolve_position(
        [entry.name for entry in existing], Placement(PLACE_FIRST), "New"
    )
    analysis = analyze_placement(
        existing, candidate("@lists.example.com"), at_index=at_index
    )

    assert [finding.narrow for finding in analysis.starves] == ["Announce"]
    assert analysis.starves[0].certainty == CERTAIN
    assert not analysis.dead_on_arrival


# ----------------------------------------------------------------------------
def test_appending_the_same_rule_starves_nothing():
    """Same rule, same script, opposite verdict -- decided by the index.

    This is the pair that proves the index is load-bearing: appended, the
    broad rule sits behind the narrow one and takes only what is left over,
    so there is nothing to warn about. A ``warn_about_placement`` that
    ignored the flags would report this for both, and the ``--first`` case
    above would go unmentioned.
    """
    existing = build(
        rule("Announce", 'header :contains "to" "announce@lists.example.com"')
    )

    at_index = resolve_position(
        [entry.name for entry in existing], None, "New"
    )
    analysis = analyze_placement(
        existing, candidate("@lists.example.com"), at_index=at_index
    )

    assert not analysis.starves


# ----------------------------------------------------------------------------
def test_placing_a_rule_behind_a_broad_stop_reports_it_dead_on_arrival():
    """The other direction, which appending could already reach.

    Included because the resolver now supplies the index for both, and a
    resolver bug that shifted every answer by one would still leave the
    starve test above passing while silently moving this boundary.
    """
    existing = build(
        rule("Lists", 'header :contains "to" "@lists.example.com"')
    )

    at_index = resolve_position(
        [entry.name for entry in existing],
        Placement(PLACE_AFTER, "Lists"),
        "New",
    )
    analysis = analyze_placement(
        existing,
        candidate("announce@lists.example.com"),
        at_index=at_index,
    )

    assert at_index == 1
    assert analysis.dead_on_arrival[0].broad == "Lists"
    assert analysis.dead_on_arrival[0].certainty == CERTAIN


# ----------------------------------------------------------------------------
def test_placing_a_rule_ahead_of_a_broad_stop_reports_nothing_dead():
    """``--before`` is how a user rescues a rule from a broad earlier one.

    The same two rules as the test above, with the new one moved in front:
    nothing precedes it, so nothing can stop it running. Without this the
    suite would pin only the failing arrangement, and a resolver that
    always answered "end" would still look correct.
    """
    existing = build(
        rule("Lists", 'header :contains "to" "@lists.example.com"')
    )

    at_index = resolve_position(
        [entry.name for entry in existing],
        Placement(PLACE_BEFORE, "Lists"),
        "New",
    )
    analysis = analyze_placement(
        existing,
        candidate("announce@lists.example.com"),
        at_index=at_index,
    )

    assert at_index == 0
    assert not analysis.dead_on_arrival

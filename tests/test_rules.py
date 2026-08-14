"""Reading existing rules, and reasoning about what a new one would do.

Two things are being proved here, and the second matters more than the
first. That the reduction reads a real Roundcube-shaped script correctly is
table stakes. That the containment analysis **refuses to over-claim** is the
point: a CERTAIN finding says a rule is unreachable, which is actionable, and
a POSSIBLE finding says it might be, which is advice. Every test that asserts
POSSIBLE rather than CERTAIN is guarding that boundary, not settling for a
weaker answer.
"""

import pytest

from mxfilter.criteria import Criteria
from mxfilter.rules import (
    CERTAIN,
    POSSIBLE,
    analyze_placement,
    audit,
    read_rules,
    rule_from_criteria,
)
from mxfilter.sieve import parse_script

# ############################################################################
# Fixtures
# ############################################################################

# Shaped like the live account's script: Roundcube name markers, anyof, a
# fileinto, and stop on every rule. The stops are what make ordering matter
# at all, so a fixture without them would not exercise anything.
ROUNDCUBE_SCRIPT = """require ["fileinto"];
# rule:[Github Notifications]
if anyof (header :contains "from" "notifications@github.com")
{
\tfileinto "INBOX.Github.Notifications";
\tstop;
}
# rule:[Trash Herrschners]
if anyof (header :contains "to" "herrschners@harleypig.com")
{
\tfileinto "INBOX.Trash";
\tstop;
}
"""


# ----------------------------------------------------------------------------
def build(*rules: str) -> list:
    """Parse a script assembled from rule bodies and reduce it to Rules."""
    body = "\n".join(rules)

    return read_rules(parse_script(f'require ["fileinto"];\n{body}'))


# ----------------------------------------------------------------------------
def rule(name: str, test: str, stop: bool = True) -> str:
    """Return one Sieve rule with a Roundcube name marker."""
    tail = "\n\tstop;" if stop else ""

    return (
        f'# rule:[{name}]\nif {test}\n{{\n\tfileinto "INBOX.{name}";{tail}\n}}'
    )


# ----------------------------------------------------------------------------
def candidate(
    header: str = "to",
    value: str = "announce@lists.example.com",
    compare: str = "contains",
    match: str = "any",
    stops: bool = True,
    name: str = "New",
):
    """Return a Rule for something not in the script yet."""
    criteria = Criteria(match=match, compare=compare)
    criteria.add(header, value)

    return rule_from_criteria(name, criteria, ("fileinto",), stops=stops)


# ############################################################################
# Reading
# ############################################################################


# ----------------------------------------------------------------------------
def test_reads_names_order_and_stop():
    """A real-shaped script reduces to ordered, named, stop-aware rules."""
    rules = read_rules(parse_script(ROUNDCUBE_SCRIPT))

    assert [entry.name for entry in rules] == [
        "Github Notifications",
        "Trash Herrschners",
    ]

    assert [entry.index for entry in rules] == [0, 1]
    assert all(entry.stops for entry in rules)
    assert rules[0].actions == ("fileinto", "stop")


# ----------------------------------------------------------------------------
def test_reads_test_values_unquoted():
    """Header and key arrive as values, not as quoted Sieve source."""
    first = read_rules(parse_script(ROUNDCUBE_SCRIPT))[0]

    assert len(first.tests) == 1
    assert first.tests[0].header == "From"
    assert first.tests[0].match_type == "contains"
    assert first.tests[0].keys == ("notifications@github.com",)


# ----------------------------------------------------------------------------
def test_a_rule_without_stop_is_marked_as_such():
    """``stops`` is False when evaluation would continue past the rule."""
    rules = build(rule("Keeps Going", 'header :contains "to" "a@x"', False))

    assert rules[0].stops is False
    assert "stop" not in rules[0].actions


# ----------------------------------------------------------------------------
def test_multi_header_test_expands_to_one_test_per_header():
    """``header :contains ["to","cc"]`` is two comparable tests, not one."""
    rules = build(rule("Both", 'header :contains ["to","cc"] "a@x.com"'))

    assert [test.header for test in rules[0].tests] == ["To", "Cc"]


# ----------------------------------------------------------------------------
def test_unmodelled_condition_is_recorded_not_guessed():
    """A condition outside the model is flagged rather than approximated."""
    rules = build(rule("Catch All", "true"))

    assert rules[0].unmodelled == ("true",)
    assert rules[0].modelled is False


# ----------------------------------------------------------------------------
def test_read_rules_rejects_a_raw_script():
    """The argument is a parsed filter set, and the error says so."""
    with pytest.raises(Exception) as excinfo:
        read_rules(ROUNDCUBE_SCRIPT)

    assert "parse_script" in str(excinfo.value)


# ############################################################################
# Dead on arrival -- an earlier rule swallowing a new one
# ############################################################################


# ----------------------------------------------------------------------------
def test_substring_containment_is_certain():
    """The decidable case: a broader needle, same header, both contains.

    Any To header containing ``announce@lists.example.com`` necessarily
    contains ``@lists.example.com``, so the earlier rule's stop makes the
    new rule unreachable. This is a fact, not an estimate, which is why it
    is allowed to be CERTAIN.
    """
    existing = build(
        rule("Lists", 'header :contains "to" "@lists.example.com"')
    )

    analysis = analyze_placement(existing, candidate())

    assert len(analysis.dead_on_arrival) == 1
    assert analysis.dead_on_arrival[0].certainty == CERTAIN
    assert analysis.dead_on_arrival[0].broad == "Lists"
    assert analysis.certain()


# ----------------------------------------------------------------------------
def test_no_stop_means_no_shadow():
    """Without ``stop`` the earlier rule cannot make anything unreachable."""
    existing = build(
        rule("Lists", 'header :contains "to" "@lists.example.com"', False)
    )

    assert not analyze_placement(existing, candidate())


# ----------------------------------------------------------------------------
def test_unrelated_header_is_not_reported_at_all():
    """No shared header and no overlap means silence, not a warning.

    Warning about every pair would make the warnings worthless.
    """
    existing = build(rule("Subj", 'header :contains "subject" "invoice"'))

    assert not analyze_placement(existing, candidate())


# ----------------------------------------------------------------------------
def test_narrower_earlier_rule_is_only_possible():
    """Containment the other way round is not containment.

    ``announce@lists.example.com`` does not cover ``@lists.example.com``:
    plenty of list mail is not from that one address. Same header, so it is
    worth mentioning -- but only as POSSIBLE.
    """
    existing = build(
        rule("Announce", 'header :contains "to" "announce@lists.example.com"')
    )

    analysis = analyze_placement(
        existing, candidate(value="@lists.example.com")
    )

    assert analysis.dead_on_arrival[0].certainty == POSSIBLE
    assert not analysis.certain()


# ----------------------------------------------------------------------------
def test_broad_is_against_narrow_contains_is_not_certain():
    """``:is`` cannot cover ``:contains`` -- the value could be anything."""
    existing = build(rule("Exact", 'header :is "to" "a@x.com"'))

    analysis = analyze_placement(existing, candidate(value="a@x.com"))

    assert analysis.dead_on_arrival[0].certainty == POSSIBLE


# ----------------------------------------------------------------------------
def test_broad_contains_against_narrow_is_is_certain():
    """A value that *is* the key also contains any substring of it."""
    existing = build(rule("Lists", 'header :contains "to" "@lists.example"'))

    analysis = analyze_placement(
        existing, candidate(value="me@lists.example", compare="is")
    )

    assert analysis.dead_on_arrival[0].certainty == CERTAIN


# ----------------------------------------------------------------------------
def test_glob_is_never_certain():
    """``:matches`` containment is undecidable here, so it stays advice."""
    existing = build(
        rule("Glob", 'header :matches "to" "*@lists.example.com"')
    )

    analysis = analyze_placement(existing, candidate())

    assert analysis.dead_on_arrival[0].certainty == POSSIBLE


# ----------------------------------------------------------------------------
def test_unmodelled_earlier_rule_warns_rather_than_stays_silent():
    """A condition we cannot read is a reason to warn, not to say nothing."""
    existing = build(rule("Catch All", "true"))

    analysis = analyze_placement(existing, candidate())

    assert analysis.dead_on_arrival[0].certainty == POSSIBLE
    assert "does not model" in analysis.dead_on_arrival[0].reason


# ----------------------------------------------------------------------------
def test_every_key_of_an_anyof_must_be_covered():
    """One uncovered alternative is enough to keep the new rule reachable."""
    existing = build(
        rule("Lists", 'header :contains "to" "@lists.example.com"')
    )

    criteria = Criteria(match="any")
    criteria.add("to", "announce@lists.example.com")
    criteria.add("to", "someone@elsewhere.test")

    analysis = analyze_placement(
        existing, rule_from_criteria("New", criteria, ("fileinto",))
    )

    assert analysis.dead_on_arrival[0].certainty == POSSIBLE


# ----------------------------------------------------------------------------
def test_one_covered_term_is_enough_for_an_allof():
    """An ``allof`` fires only when every term does, so covering one wins."""
    existing = build(
        rule("Lists", 'header :contains "to" "@lists.example.com"')
    )

    criteria = Criteria(match="all")
    criteria.add("to", "announce@lists.example.com")
    criteria.add("subject", "release")

    analysis = analyze_placement(
        existing, rule_from_criteria("New", criteria, ("fileinto",))
    )

    assert analysis.dead_on_arrival[0].certainty == CERTAIN


# ----------------------------------------------------------------------------
def test_an_allof_earlier_rule_is_never_certain():
    """A broad ``allof`` needs its own other terms to fire on the message."""
    existing = build(
        rule(
            "Both",
            'allof (header :contains "to" "@lists.example.com", '
            'header :contains "subject" "weekly")',
        )
    )

    analysis = analyze_placement(existing, candidate())

    assert analysis.dead_on_arrival[0].certainty == POSSIBLE


# ############################################################################
# Starving -- a new rule swallowing existing ones
# ############################################################################


# ----------------------------------------------------------------------------
def test_inserting_first_starves_a_later_rule():
    """The worse direction: the damage lands on rules already relied on."""
    existing = build(
        rule("Announce", 'header :contains "to" "announce@lists.example.com"')
    )

    analysis = analyze_placement(
        existing, candidate(value="@lists.example.com"), at_index=0
    )

    assert len(analysis.starves) == 1
    assert analysis.starves[0].certainty == CERTAIN
    assert analysis.starves[0].narrow == "Announce"
    assert not analysis.dead_on_arrival


# ----------------------------------------------------------------------------
def test_appending_starves_nothing():
    """Current behaviour: appended rules cannot starve what precedes them."""
    existing = build(
        rule("Announce", 'header :contains "to" "announce@lists.example.com"')
    )

    analysis = analyze_placement(
        existing, candidate(value="@lists.example.com")
    )

    assert not analysis.starves


# ----------------------------------------------------------------------------
def test_a_new_rule_without_stop_starves_nothing():
    """No ``stop`` means later rules still get their turn."""
    existing = build(
        rule("Announce", 'header :contains "to" "announce@lists.example.com"')
    )

    analysis = analyze_placement(
        existing,
        candidate(value="@lists.example.com", stops=False),
        at_index=0,
    )

    assert not analysis.starves


# ----------------------------------------------------------------------------
def test_the_live_ruleset_shape_is_order_independent_today():
    """The account's real rules do not overlap, so order does not yet bite.

    The before half of a pair. It is worth pinning because the next test is
    what changes it, and a regression that made these two overlap would
    otherwise be invisible.
    """
    existing = read_rules(parse_script(ROUNDCUBE_SCRIPT))

    analysis = analyze_placement(
        existing, candidate(header="subject", value="invoice"), at_index=0
    )

    assert not analysis


# ----------------------------------------------------------------------------
def test_a_broad_github_rule_makes_that_ruleset_order_sensitive():
    """The after half: the change that first makes placement matter here.

    ``from :contains "github.com"`` covers the existing notifications rule
    outright, so appending it is harmless and inserting it first would
    silently take the notification filing away.
    """
    existing = read_rules(parse_script(ROUNDCUBE_SCRIPT))
    broad = candidate(header="from", value="github.com", name="Github")

    appended = analyze_placement(existing, broad)
    inserted = analyze_placement(existing, broad, at_index=0)

    assert not appended.starves
    assert not appended.certain()

    assert inserted.starves[0].certainty == CERTAIN
    assert inserted.starves[0].narrow == "Github Notifications"


# ############################################################################
# Regressions found by running this against the real account
# ############################################################################


# ----------------------------------------------------------------------------
def test_a_single_condition_allof_is_read_as_a_disjunction():
    """Roundcube writes one condition as ``allof``, and it must still count.

    Found by running ``mxfilter rules`` against the live script rather than
    by a test: every rule Roundcube's UI had written was a one-test
    ``allof``, and treating ``allof`` as undecidable made the analysis
    refuse to decide anything at all on exactly the scripts it exists to
    read.

    ``allof`` and ``anyof`` over a single test are the same predicate.
    """
    existing = build(
        rule("Lists", 'allof (header :contains "to" "@lists.example.com")')
    )

    analysis = analyze_placement(existing, candidate())

    assert analysis.dead_on_arrival[0].certainty == CERTAIN


# ----------------------------------------------------------------------------
def test_distinct_addresses_on_one_header_are_not_warned_about():
    """Sharing a header is not grounds for a warning. Overlap is.

    Also found by running against the live script: three of its four rules
    file distinct addresses out of ``To``, and an earlier draft flagged
    every pair of them. A check that fires on three quarters of a correct
    rule set is not cautious -- nobody reads the fourth warning.

    These two can still both match a message addressed to both recipients.
    That is a real thing and a much rarer one than a rule shadowing
    another, and warning about it on every pair costs more than it finds.
    """
    existing = build(
        rule("Herrschners", 'allof (header :contains "to" "a@example.com")'),
        rule("Rumble", 'allof (header :contains "to" "b@example.com")'),
        rule("Arch", 'allof (header :contains "to" "c@lists.example.org")'),
    )

    assert audit(existing) == []


# ----------------------------------------------------------------------------
def test_overlapping_addresses_on_one_header_still_warn():
    """The other half of the pair: a real overlap is still reported.

    The earlier rule is the *narrower* of the two, so it cannot cover the
    new one and this is not CERTAIN. But its key sits inside the new rule's
    key, so any mail the earlier rule catches is mail the new rule wanted
    -- a genuine bite out of it, and worth the line it takes.

    This is what separates the fix above from simply warning less: silence
    for unrelated addresses, a warning for related ones.
    """
    existing = build(
        rule(
            "Announce",
            'allof (header :contains "to" "announce@lists.example.com")',
        )
    )

    analysis = analyze_placement(existing, candidate(value="@lists.example"))

    assert analysis.dead_on_arrival[0].certainty == POSSIBLE

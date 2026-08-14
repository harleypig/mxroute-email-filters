"""Adversarial tests for the containment analysis: hunting a false CERTAIN.

``test_rules.py`` proves the module does the right thing on the shapes it
was written for. This file assumes it does not, and goes looking.

The one property worth attacking is the certainty split. A POSSIBLE finding
costs a reader a moment's dismissal. A CERTAIN finding says "this rule can
never fire", which someone will act on -- by deleting the rule, or by not
adding the one they came to add -- so a CERTAIN that is wrong loses mail
filing silently and blames the user for it. Missing a real CERTAIN costs
nothing but a warning that reads as advice. The two errors are not
symmetric, and everything below is written from that asymmetry.

So the tests here fall into two kinds, and both are the point:

* **Over-claims.** Inputs where ``_covers`` returns CERTAIN and the Sieve
  server would disagree. These are marked ``xfail`` and asserted against
  the answer the module *should* give, so fixing the module turns them
  green rather than leaving them to be rewritten.
* **Boundaries that hold.** Inputs that look like they should break it and
  do not. These pass, and they are worth their line count precisely
  because "we tried this and it was fine" is a different statement from
  "nobody has tried this".
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
from mxfilter.sieve import merge_rule, parse_script

# ############################################################################
# Fixtures
# ############################################################################


# ----------------------------------------------------------------------------
def build(*bodies: str) -> list:
    """Parse a script assembled from rule bodies and reduce it to Rules."""
    body = "\n".join(bodies)

    return read_rules(parse_script(f'require ["fileinto"];\n{body}'))


# ----------------------------------------------------------------------------
def rule(
    name: str,
    test: str,
    stop: bool = True,
    actions: str | None = None,
) -> str:
    """Return one Sieve rule with a Roundcube name marker.

    ``actions`` replaces the whole rule body, for the tests that care where
    ``stop`` sits relative to everything else; ``stop`` is the shorthand for
    the ordinary case.
    """
    if actions is None:
        tail = "\n\tstop;" if stop else ""
        actions = f'\tfileinto "INBOX.{name}";{tail}'

    return f"# rule:[{name}]\nif {test}\n{{\n{actions}\n}}"


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
# The comparator -- casefold() is not i;ascii-casemap
# ############################################################################


# ----------------------------------------------------------------------------
def test_ascii_case_is_ignored_in_both_positions():
    """ASCII case must not affect the verdict, because Sieve ignores it.

    Sieve's default comparator is ``i;ascii-casemap``, so a rule written
    with a shouty key catches lowercase mail and vice versa. This is the
    half of case handling the module gets right, and it is pinned first so
    the two xfails below read as a boundary being crossed rather than as
    case-insensitivity being wrong in general.
    """
    existing = build(
        rule("Lists", 'header :contains "to" "@LISTS.EXAMPLE.COM"')
    )

    analysis = analyze_placement(existing, candidate())

    assert analysis.dead_on_arrival[0].certainty == CERTAIN


# ----------------------------------------------------------------------------
def test_non_ascii_folding_must_not_make_contains_certain():
    """A German eszett is not two letters s to a Sieve server.

    ``"ß".casefold()`` is ``"ss"``, so the module decides that any header
    containing the narrow key also contains the broad one. Under
    ``i;ascii-casemap`` it does not: ``Subject: Strasse ist Straße`` aside,
    a subject of ``Straße`` matches ``:contains "ß"`` and does **not**
    match ``:contains "ss"``, so the earlier rule never fires and the later
    one is perfectly reachable.

    The same mechanism bites without any change of length -- U+212A KELVIN
    SIGN folds to ``k``, and the Turkish dotted capital I folds to ``i``
    plus a combining dot -- so a fix has to be an ASCII-only fold, not a
    special case for this character.
    """
    existing = build(rule("Double S", 'header :contains "subject" "ss"'))

    analysis = analyze_placement(
        existing, candidate(header="subject", value="ß")
    )

    assert analysis.dead_on_arrival[0].certainty == POSSIBLE


# ----------------------------------------------------------------------------
def test_non_ascii_folding_must_not_make_is_certain():
    """Equality under casefold is not equality under i;ascii-casemap.

    ``"STRASSE".casefold() == "straße".casefold()``, so the module treats
    the two ``:is`` keys as the same key. A Sieve server comparing with
    ``i;ascii-casemap`` sees two different strings, so a message whose
    subject is ``straße`` matches only the second rule.

    Worth its own test rather than folding into the one above: the two
    branches of ``_key_covered`` are separate code, and a fix applied to
    only the substring one would leave this green-looking and wrong.
    """
    existing = build(rule("Exact", 'header :is "subject" "STRASSE"'))

    analysis = analyze_placement(
        existing, candidate(header="subject", value="straße", compare="is")
    )

    assert analysis.dead_on_arrival[0].certainty == POSSIBLE


# ----------------------------------------------------------------------------
def test_an_explicit_comparator_must_not_be_ignored():
    """``:comparator "i;octet"`` makes a test case-sensitive; we drop it.

    ``i;octet`` compares bytes, so the earlier rule here matches only a
    lowercase ``abc``. The later rule, on the default comparator, also
    matches ``ABC`` -- a message the earlier rule lets through. The module
    reduces both to the same ``HeaderTest`` and calls the second one dead.

    Not exotic: ``i;octet`` is one of the two comparators RFC 5228 requires
    every implementation to have, needs no ``require`` line, and is what
    someone reaches for when they want case to matter.
    """
    existing = build(
        rule("Octet", 'header :comparator "i;octet" :contains "subject" "abc"')
    )

    analysis = analyze_placement(
        existing, candidate(header="subject", value="ABC")
    )

    assert analysis.dead_on_arrival[0].certainty == POSSIBLE


# ############################################################################
# Escaped and empty keys
# ############################################################################


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    [
        "back\\slash",
        'quo"te',
        "\\Seen",
        'both \\ and " here',
        "a\\\\b",
    ],
)
def test_escapes_survive_the_round_trip_into_a_comparable_key(value):
    """A key comes back out of a rendered script as the value that went in.

    ``escape_sieve_string`` doubles backslashes and escapes quotes on the
    way out; ``_unquote`` reverses both on the way back. If it reversed
    them in the wrong order, or missed one, every comparison involving such
    a key would be against a string the user never typed -- and a
    containment answer computed from the wrong string is exactly the silent
    over-claim this module exists to avoid.

    Rendered through ``merge_rule`` rather than hand-written so the test
    covers the real path the escaping takes, not an approximation of it.
    """
    criteria = Criteria(match="any", compare="contains")
    criteria.add("subject", value)

    script = merge_rule(
        'require ["fileinto"];',
        "Escaped",
        criteria.sieve_conditions(),
        [("fileinto", "INBOX.Escaped")],
        criteria.sieve_matchtype(),
    )

    assert read_rules(parse_script(script))[0].tests[0].keys == (value,)


# ----------------------------------------------------------------------------
def test_containment_still_works_on_an_escaped_key():
    """A backslash key compares as a plain substring once unescaped.

    ``\\Seen`` starts with a literal backslash, so a rule keyed on a bare
    backslash covers it. The point is that containment is decided on the
    unescaped values -- comparing the Sieve source instead would see four
    backslashes against two and answer differently.
    """
    existing = build(
        rule("Any Backslash", 'header :contains "subject" "\\\\"')
    )

    analysis = analyze_placement(
        existing, candidate(header="subject", value="\\Seen")
    )

    assert analysis.dead_on_arrival[0].certainty == CERTAIN


# ----------------------------------------------------------------------------
def test_an_empty_contains_key_covers_the_same_header():
    """``:contains ""`` on a header genuinely does cover that header.

    RFC 5228 is explicit that an existing header contains the null key, so
    ``header :contains "to" ""`` fires for every message that has a ``To``
    at all. Any message the later ``To`` rule fires for necessarily has
    one, so CERTAIN is the correct answer here rather than an accident of
    ``"" in anything``.

    ``Criteria.add`` refuses an empty value, so this shape can only arrive
    from a script someone else wrote -- which is precisely the input the
    module has to survive.
    """
    existing = build(rule("Has A To", 'header :contains "to" ""'))

    analysis = analyze_placement(existing, candidate())

    assert analysis.dead_on_arrival[0].certainty == CERTAIN


# ----------------------------------------------------------------------------
def test_an_empty_contains_key_claims_nothing_about_another_header():
    """The null key covers its own header, not every message.

    A ``header`` test fires only when the named header is present, so
    ``header :contains "to" ""`` says nothing about a message filtered on
    ``Subject`` -- mail with no ``To`` header exists. The module stays
    silent rather than reaching for "matches everything", which is the
    conservative direction and the right one.
    """
    existing = build(rule("Has A To", 'header :contains "to" ""'))

    assert not analyze_placement(
        existing, candidate(header="subject", value="invoice")
    )


# ############################################################################
# Shape reduction -- what survives being flattened
# ############################################################################


# ----------------------------------------------------------------------------
def test_a_multi_header_test_inside_an_allof_must_not_be_certain():
    """``["to","cc"]`` is an OR, and an ``allof`` turns it into an AND.

    The later rule fires when ``(To OR Cc) AND Subject``. Flattened, the
    module models it as ``To AND Cc AND Subject`` -- and since covering any
    single conjunct of an ``allof`` is enough for CERTAIN, covering the
    ``To`` half is taken as covering the rule.

    It does not. A message with ``Cc: x@example.com``, ``Subject: weekly``
    and some other ``To`` matches the later rule and misses the earlier
    one, so the later rule is live and the module calls it dead.

    Reached through ``audit`` because that is where a user meets it: the
    script is already on the server, and this is the answer to "which of my
    rules are dead?".
    """
    findings = audit(
        build(
            rule("Broad", 'header :contains "to" "x@example.com"'),
            rule(
                "Narrow",
                'allof (header :contains ["to","cc"] "x@example.com", '
                'header :contains "subject" "weekly")',
            ),
        )
    )

    assert findings[0].certainty == POSSIBLE


# ----------------------------------------------------------------------------
def test_a_multi_header_test_inside_an_anyof_reduces_correctly():
    """The same flattening is exactly right under ``anyof``.

    An OR of headers nested in an OR of tests is one flat OR, so splitting
    it loses nothing. Pinned next to the failure above because the two
    share a code path and differ only in the combinator -- whoever fixes
    that one must not "fix" this one, which is already correct.
    """
    findings = audit(
        build(
            rule(
                "Broad", 'anyof (header :contains ["to","cc"] "@lists.test")'
            ),
            rule("Narrow", 'header :contains "cc" "announce@lists.test"'),
        )
    )

    assert findings[0].certainty == CERTAIN
    assert findings[0].narrow == "Narrow"


# ----------------------------------------------------------------------------
def test_every_key_of_a_multi_key_narrow_test_must_be_covered():
    """A key list is an OR, so one uncovered key keeps the rule alive.

    The later rule fires on either address; the earlier one covers only the
    first. Mail to the second still reaches it, so the answer is advice,
    not a fact.
    """
    findings = audit(
        build(
            rule("Broad", 'header :contains "to" "@lists.test"'),
            rule(
                "Narrow",
                'header :contains "to" ["a@lists.test","b@elsewhere.test"]',
            ),
        )
    )

    assert findings[0].certainty == POSSIBLE


# ----------------------------------------------------------------------------
def test_one_key_of_a_multi_key_broad_test_is_enough_to_cover():
    """The same OR, read from the other side, needs only one key to hit.

    The mirror of the test above, and the reason both are here: the two
    positions are handled by different lines of ``_key_covered``, and
    getting one of them backwards would be invisible without the pair.
    """
    existing = build(
        rule(
            "Broad",
            'header :contains "to" ["@lists.test","@groups.test"]',
        )
    )

    analysis = analyze_placement(existing, candidate(value="me@groups.test"))

    assert analysis.dead_on_arrival[0].certainty == CERTAIN


# ----------------------------------------------------------------------------
def test_a_nested_combinator_is_recorded_not_flattened():
    """An ``allof`` inside an ``anyof`` is left unmodelled, so never CERTAIN.

    Flattening it would be the same class of error as the multi-header
    ``allof`` above, and this is where the module gets it right: the nested
    test is put in ``unmodelled``, which disqualifies the rule from any
    confident verdict in either direction.
    """
    rules = build(
        rule(
            "Nested",
            'anyof (allof (header :contains "to" "a@b.test", '
            'header :contains "subject" "x"), '
            'header :contains "to" "c@d.test")',
        )
    )

    assert rules[0].unmodelled == ("allof",)
    assert rules[0].modelled is False

    analysis = analyze_placement(rules, candidate(value="a@b.test"))

    assert analysis.dead_on_arrival[0].certainty == POSSIBLE


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("test", "recorded"),
    [
        ('address :contains "from" "a@b.test"', "address"),
        ("size :over 1M", "size"),
        ('exists "x-spam-flag"', "exists"),
        ('not header :contains "to" "a@b.test"', "not"),
    ],
)
def test_a_test_that_is_not_a_header_test_is_unmodelled(test, recorded):
    """Anything that is not ``header`` is recorded, never approximated.

    ``address`` is the one that matters: it reads almost identically to
    ``header``, it is common in hand-written scripts, and it tests the
    parsed address rather than the raw header value -- so treating it as a
    ``header`` test would produce confident answers about different
    semantics. ``not`` is the other direction of the same risk: dropping it
    would invert a rule's meaning while leaving the keys intact.
    """
    rules = build(rule("Odd", test))

    assert rules[0].tests == ()
    assert rules[0].unmodelled == (recorded,)


# ############################################################################
# Actions -- what counts as stopping
# ############################################################################


# ----------------------------------------------------------------------------
def test_an_action_after_stop_does_not_change_that_the_rule_stops():
    """``stop`` ends evaluation wherever it sits in the rule body.

    A ``fileinto`` written after it is dead code, but the rule still stops,
    and shadowing depends only on that. Reading ``stops`` off the position
    of ``stop`` rather than its presence would miss this and quietly report
    no shadow at all.
    """
    rules = build(
        rule(
            "Backwards",
            'header :contains "to" "a@b.test"',
            actions='\tstop;\n\tfileinto "INBOX.Late";',
        )
    )

    assert rules[0].actions == ("stop", "fileinto")
    assert rules[0].stops is True


# ----------------------------------------------------------------------------
def test_a_rule_whose_only_action_is_stop_still_shadows():
    """A bare ``stop`` files nothing and still kills everything after it.

    The most destructive rule someone can write, and the easiest to
    overlook when scanning a script, because it has no folder name in it to
    catch the eye.
    """
    existing = build(
        rule(
            "Drop Lists",
            'header :contains "to" "@lists.example.com"',
            actions="\tstop;",
        )
    )

    analysis = analyze_placement(existing, candidate())

    assert analysis.dead_on_arrival[0].certainty == CERTAIN


# ############################################################################
# Degenerate inputs
# ############################################################################


# ----------------------------------------------------------------------------
def test_an_empty_script_analyses_and_audits_to_nothing():
    """No rules is a normal starting state, not an error.

    A fresh account has no active script at all, and the first ``add`` runs
    the placement analysis against nothing. It has to come back empty
    rather than raise.
    """
    assert read_rules(parse_script("")) == []
    assert not analyze_placement([], candidate())
    assert audit([]) == []


# ----------------------------------------------------------------------------
def test_a_candidate_with_no_criteria_claims_nothing():
    """An empty candidate must not be swallowed by a vacuous ``all()``.

    ``_covers`` decides an ``anyof`` narrow rule by ``all(covered)``, and
    ``all([])`` is True -- so a candidate with no tests would be reported
    CERTAIN-dead against any earlier rule if nothing else stopped it. The
    ``modelled`` guard is what does, and this is the test that says so.

    The CLI rejects empty criteria before it gets here, which is exactly
    why the guard needs a test: nothing else would notice it going.
    """
    empty = rule_from_criteria("Empty", Criteria(), ("fileinto",), stops=True)
    existing = build(rule("Broad", 'header :contains "to" "@lists.test"'))

    assert not analyze_placement(existing, empty)
    assert not analyze_placement(existing, empty, at_index=0)


# ----------------------------------------------------------------------------
def test_an_at_index_past_the_end_behaves_like_appending():
    """An out-of-range position is clamped by slicing, not rejected.

    Every existing rule ends up before the candidate and none after it, so
    the answer matches a plain append. Pinned because ``at_index`` is not
    range-checked, and silently doing the sensible thing is only sensible
    if it is deliberate.
    """
    existing = build(
        rule("Announce", 'header :contains "to" "announce@lists.test"')
    )

    analysis = analyze_placement(
        existing, candidate(value="@lists.test"), at_index=99
    )

    assert not analysis.starves
    assert analysis.dead_on_arrival[0].certainty == POSSIBLE


# ----------------------------------------------------------------------------
def test_a_negative_at_index_means_the_front_not_the_end():
    """``max(0, at_index)`` reads -1 as first, and -1 is a plausible slip.

    ``rule_from_criteria`` defaults its ``index`` to -1 for a rule not in
    the script yet, so passing that straight through as ``at_index`` is an
    easy mistake to make -- and it means the opposite of what -1 means in
    every other Python sequence. The behaviour is defensible; going
    unnoticed is not.
    """
    existing = build(
        rule("Announce", 'header :contains "to" "announce@lists.test"')
    )

    analysis = analyze_placement(
        existing, candidate(value="@lists.test"), at_index=-1
    )

    assert analysis.starves[0].narrow == "Announce"
    assert analysis.starves[0].certainty == CERTAIN


# ############################################################################
# Never CERTAIN -- the guarantees the tiering rests on
# ############################################################################


# ----------------------------------------------------------------------------
def test_a_glob_in_the_narrow_position_is_never_certain():
    """``:matches`` is undecidable here whichever side it is on.

    The existing suite pins the broad side. This is the other one, and it
    is the more tempting of the two to decide: ``*@lists.example.com``
    looks like it is obviously covered by ``:contains "@lists.example.com"``
    -- and it is, but only because a human read the glob. Deciding that in
    general is glob-language containment, which the module does not
    attempt, so it must not answer it in the easy case either.
    """
    existing = build(
        rule("Lists", 'header :contains "to" "@lists.example.com"')
    )

    analysis = analyze_placement(
        existing, candidate(value="*@lists.example.com", compare="matches")
    )

    assert analysis.dead_on_arrival[0].certainty == POSSIBLE


# ----------------------------------------------------------------------------
def test_a_glob_candidate_starves_nothing_with_certainty():
    """The same refusal in the starve direction, where the cost is higher.

    A wrong CERTAIN on dead-on-arrival talks the user out of a rule they do
    not have yet. A wrong CERTAIN on starving talks them out of one that is
    already filing their mail.
    """
    existing = build(
        rule("Announce", 'header :contains "to" "announce@lists.test"')
    )

    analysis = analyze_placement(
        existing,
        candidate(value="*@lists.test", compare="matches"),
        at_index=0,
    )

    assert analysis.starves[0].certainty == POSSIBLE


# ----------------------------------------------------------------------------
def test_an_unmodelled_narrow_rule_is_never_certain():
    """Not understanding the *later* rule disqualifies CERTAIN too.

    The existing suite covers an unmodelled earlier rule. This is the
    other half, and it is the one a naive reading would get wrong: the
    broad rule is fully understood and plainly covers the part of the
    narrow rule that was read, so it is easy to conclude the narrow rule is
    dead. It is not -- the part that was not read is an ``anyof``
    alternative that can fire on its own.
    """
    findings = audit(
        build(
            rule("Broad", 'header :contains "to" "@lists.test"'),
            rule(
                "Narrow",
                'anyof (header :contains "to" "announce@lists.test", '
                "size :over 5M)",
            ),
        )
    )

    assert findings[0].certainty == POSSIBLE
    assert "does not model" in findings[0].reason


# ----------------------------------------------------------------------------
def test_audit_only_ever_names_the_earlier_rule_as_the_shadower():
    """Order decides which of two overlapping rules wins, and only order.

    This is the well-ordered script -- specific first, general second --
    with the containment running the wrong way for a confident verdict:
    ``announce@lists.test`` does not cover ``@lists.test``. So the only
    honest output is advice, attributed to the rule that runs first.

    Two things would break it, and both would be quiet. ``audit`` reuses
    ``analyze_placement``, which also knows how to report starving, so a
    slice off by one would attribute the finding to the *later* rule and
    tell the user to delete the one that is actually working. And treating
    the overlap as decided would call a live rule dead.
    """
    findings = audit(
        build(
            rule("Narrow", 'header :contains "to" "announce@lists.test"'),
            rule("Broad", 'header :contains "to" "@lists.test"'),
        )
    )

    assert [finding.broad for finding in findings] == ["Narrow"]
    assert [finding.narrow for finding in findings] == ["Broad"]
    assert not [f for f in findings if f.certainty == CERTAIN]

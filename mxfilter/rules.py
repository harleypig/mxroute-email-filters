"""Reading the rules that are already there, and reasoning about them.

``sieve.parse_script`` gives back a ``sievelib`` filter set, which is the
right shape for *editing* a script and the wrong shape for *thinking about*
one: the tests are command objects whose arguments are still quoted Sieve
source. This module reduces a parsed script to a flat, comparable model --
one :class:`Rule` per filter, in evaluation order, each carrying its tests,
its actions, and whether it ends with ``stop``.

Everything here is offline and returns data. Nothing prints; see
CONVENTIONS.md.

Why it exists: Sieve is **order-dependent** and ``stop`` ends evaluation, so
appending a rule can produce a rule that never fires, and inserting one can
starve rules that already work. Both currently report success. Detecting
that needs the rules compared against each other, not merely rendered.

The one thing to get right here is **not over-claiming**. Deciding whether
two arbitrary Sieve tests overlap is not something this module can do, and
guessing would be worse than saying less. So every finding carries a
:data:`CERTAIN` or :data:`POSSIBLE` marker, and the boundary between them is
enforced by :func:`_test_covers` refusing to reason about anything outside
the decidable cases. Real evaluation -- "would this rule catch *this*
message" -- is a different problem and belongs to the engine adopted in
ADR 0004, not here.
"""

from dataclasses import dataclass, field

from . import MxFilterError
from .criteria import Criteria, canonical_header

__all__ = [
    "CERTAIN",
    "POSSIBLE",
    "Analysis",
    "HeaderTest",
    "Rule",
    "Shadow",
    "analyze_placement",
    "audit",
    "read_rules",
    "rule_from_criteria",
]

# How sure a finding is. #20 insists these stay apart: an assertion that a
# rule is dead is actionable, a suspicion that it might be is advice, and
# presenting the second as the first is worse than not warning at all.
CERTAIN = "certain"
POSSIBLE = "possible"

# Sieve's own combinators, plus the marker used when a rule's condition is
# something this module did not model (a bare `true`, an `envelope` test, an
# `anyof` nested inside an `allof`). An unmodelled rule is never the basis
# of a CERTAIN finding.
ANYOF = "anyof"
ALLOF = "allof"
OTHER = "other"

# Match types this module can compare. `:matches` is deliberately absent --
# deciding whether one glob's language contains another's is a real problem,
# not an oversight, so a glob only ever yields POSSIBLE.
COMPARABLE = ("contains", "is")

# The comparator every comparison below assumes. RFC 5228 makes it the
# default when a test names none, and requires `i;octet` to exist beside it
# with no `require` line -- so a script is free to ask for byte comparison,
# under which the same two keys answer differently. A test naming any other
# comparator is read and kept, never compared; see `_comparable`.
ASCII_CASEMAP = "i;ascii-casemap"

# `i;ascii-casemap` folds A-Z, and leaves every other octet exactly alone.
#
# Do NOT replace this with `str.casefold()` or `str.lower()`, however much
# more idiomatic they look. Both implement *Unicode* case folding, which is a
# different function precisely where it matters: `"ss"` and `"ß"` fold
# together, so a rule keyed on `"ss"` would be read as covering one keyed on
# `"ß"` and a perfectly live rule reported dead -- a CERTAIN the server
# disagrees with, which is the worst thing this module can emit. It is not a
# quirk of one character either: U+212A KELVIN SIGN folds to `"k"` with no
# change of length at all. The folding function is the bug, not the input.
_CASEMAP = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
)


# ############################################################################
# Model
# ############################################################################


@dataclass(frozen=True)
class HeaderTest:
    """One ``header`` test: a header, a match type, and its key list."""

    header: str
    match_type: str
    keys: tuple[str, ...]

    # The comparator the test named, or Sieve's default when it named none.
    # Carried rather than dropped because it decides what the match type
    # *means*: `:contains` under `i;octet` is case-sensitive, so reducing a
    # byte-comparing test to the same HeaderTest as a case-insensitive one
    # makes the two look interchangeable when they are not.
    comparator: str = ASCII_CASEMAP


@dataclass(frozen=True)
class Rule:
    """One filter, reduced to what order-reasoning needs."""

    index: int
    name: str
    combinator: str
    tests: tuple[HeaderTest, ...] = ()
    actions: tuple[str, ...] = ()
    stops: bool = False

    # The parts of the condition that were not reduced to a HeaderTest.
    # Non-empty means this rule's conditions are only partly understood, so
    # it cannot support a CERTAIN verdict in either direction.
    unmodelled: tuple[str, ...] = ()

    # ------------------------------------------------------------------------
    @property
    def modelled(self) -> bool:
        """True when every part of the condition was understood."""
        return bool(self.tests) and not self.unmodelled


@dataclass(frozen=True)
class Shadow:
    """One rule making another unreachable, or suspected of it."""

    certainty: str
    broad: str
    narrow: str
    reason: str


@dataclass
class Analysis:
    """What placing a rule at a given position would mean."""

    # Rules earlier in the script that would stop this one ever running.
    dead_on_arrival: list[Shadow] = field(default_factory=list)

    # Rules later in the script that this one would stop running.
    starves: list[Shadow] = field(default_factory=list)

    # ------------------------------------------------------------------------
    def __bool__(self) -> bool:
        return bool(self.dead_on_arrival or self.starves)

    # ------------------------------------------------------------------------
    def certain(self) -> list[Shadow]:
        """Return only the findings that are decided, not suspected."""
        return [
            finding
            for finding in (*self.dead_on_arrival, *self.starves)
            if finding.certainty == CERTAIN
        ]


# ############################################################################
# Reading a parsed script
# ############################################################################


# ----------------------------------------------------------------------------
def _unquote(value: str) -> str:
    """Strip the quotes sievelib leaves on a parsed string argument.

    Parsed arguments come back as Sieve source (``'"from"'``), not as the
    value. Unescaping mirrors ``criteria.escape_sieve_string``, which is
    what put the backslashes there on the way out.
    """
    text = str(value).strip()

    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]

    return text.replace('\\"', '"').replace("\\\\", "\\")


# ----------------------------------------------------------------------------
def _as_values(argument) -> tuple[str, ...]:
    """Normalise a sievelib argument to a tuple of unquoted values.

    A single-element list and a bare string mean the same thing in Sieve
    (``"to"`` and ``["to"]``), and sievelib preserves whichever the author
    wrote, so both arrive here.
    """
    if argument is None:
        return ()

    if isinstance(argument, (list, tuple)):
        return tuple(_unquote(item) for item in argument)

    return (_unquote(argument),)


# ----------------------------------------------------------------------------
def _comparator(command) -> str:
    """Return the comparator a test named, or Sieve's default.

    sievelib splits a tagged argument in two: ``arguments["comparator"]``
    holds the ``:comparator`` tag itself and ``extra_arguments`` holds its
    value. Reading the first is what dropping the comparator looked like --
    it yields the literal ``":comparator"`` and never a comparator name, so
    the value has to come from ``extra_arguments`` or not at all.
    """
    extra = getattr(command, "extra_arguments", {}) or {}

    return _unquote(extra.get("comparator") or "") or ASCII_CASEMAP


# ----------------------------------------------------------------------------
def _header_test(command) -> HeaderTest | None:
    """Reduce a sievelib ``header`` test, or return None if it is not one.

    A ``header`` test may name several headers at once, which is a
    disjunction: Sieve tries every named header and succeeds if any of them
    matches. Only the first header is read here; the whole list is expanded
    by the caller, which is also where the decision about whether that
    expansion is faithful belongs (see :func:`_read_condition`).
    """
    if getattr(command, "name", None) != "header":
        return None

    arguments = getattr(command, "arguments", {}) or {}

    match_type = _unquote(arguments.get("match-type", ":is")).lstrip(":")
    headers = _as_values(arguments.get("header-names"))
    keys = _as_values(arguments.get("key-list"))

    if not headers or not keys:
        return None

    return HeaderTest(
        header=canonical_header(headers[0]),
        match_type=match_type,
        keys=keys,
        comparator=_comparator(command),
    )


# ----------------------------------------------------------------------------
def _expand_headers(command) -> list[HeaderTest]:
    """Return one HeaderTest per header named by a ``header`` test."""
    first = _header_test(command)

    if first is None:
        return []

    arguments = getattr(command, "arguments", {}) or {}
    headers = _as_values(arguments.get("header-names"))

    return [
        HeaderTest(
            canonical_header(name),
            first.match_type,
            first.keys,
            first.comparator,
        )
        for name in headers
    ]


# ----------------------------------------------------------------------------
def _read_condition(test) -> tuple[str, list[HeaderTest], list[str]]:
    """Split a rule's condition into combinator, tests, and leftovers.

    Anything that is not a flat ``anyof``/``allof`` of ``header`` tests --
    a nested combinator, a `size` or `envelope` test, a bare `true` -- is
    recorded in the leftovers rather than approximated. That list is what
    stops a partly-understood rule from producing a confident verdict.
    """
    if test is None:
        return OTHER, [], ["no condition"]

    name = getattr(test, "name", "") or OTHER

    if name in (ANYOF, ALLOF):
        combinator = name
        children = (getattr(test, "arguments", {}) or {}).get("tests") or []

    else:
        # A rule with one unwrapped test behaves exactly like a one-element
        # anyof, so treating it as one keeps the comparison code uniform.
        combinator = ANYOF
        children = [test]

    tests: list[HeaderTest] = []
    leftovers: list[str] = []

    for child in children:
        expanded = _expand_headers(child)

        if not expanded:
            leftovers.append(getattr(child, "name", "unknown test"))

        elif combinator == ALLOF and len(expanded) > 1:
            # `header :contains ["to","cc"] "x"` is an OR. Splitting it into
            # one test per header is faithful only where the pieces land in
            # a disjunction -- under `anyof` they join an OR that is already
            # there. Under `allof` they would become conjuncts, silently
            # rewriting `(To OR Cc) AND Subject` as `To AND Cc AND Subject`;
            # and since covering any single conjunct of an `allof` is enough
            # for CERTAIN, covering just the `To` half would condemn a rule
            # that a Cc-only message still reaches.
            #
            # A flat list of tests has nowhere to put an OR nested inside an
            # AND, so the honest move is to record what was not modelled
            # rather than approximate it -- the same treatment a nested
            # combinator already gets, and it disqualifies the rule from a
            # confident verdict in either direction.
            leftovers.append("multi-header test inside allof")

        else:
            tests.extend(expanded)

    return combinator, tests, leftovers


# ----------------------------------------------------------------------------
def _read_actions(children) -> tuple[list[str], bool]:
    """Return the action names of a rule, and whether it ends evaluation."""
    actions = [getattr(child, "name", "?") for child in children]

    return actions, "stop" in actions


# ----------------------------------------------------------------------------
def read_rules(filters) -> list[Rule]:
    """Reduce a parsed filter set to Rules, in evaluation order.

    ``filters`` is whatever ``sieve.parse_script`` returned. The index is
    the rule's position in the script, which is the only thing that decides
    which of two overlapping rules wins.
    """
    entries = getattr(filters, "filters", None)

    if entries is None:
        raise MxFilterError(
            "read_rules expects a parsed filter set from sieve.parse_script()"
        )

    rules = []

    for index, entry in enumerate(entries):
        content = entry.get("content")
        arguments = getattr(content, "arguments", {}) or {}

        combinator, tests, leftovers = _read_condition(arguments.get("test"))
        actions, stops = _read_actions(getattr(content, "children", []) or [])

        rules.append(
            Rule(
                index=index,
                name=entry.get("name", f"rule {index + 1}"),
                combinator=combinator,
                tests=tuple(tests),
                actions=tuple(actions),
                stops=stops,
                unmodelled=tuple(leftovers),
            )
        )

    return rules


# ----------------------------------------------------------------------------
def rule_from_criteria(
    name: str,
    criteria: Criteria,
    actions: tuple[str, ...] = (),
    stops: bool = False,
    index: int = -1,
) -> Rule:
    """Build a Rule for something not in the script yet.

    Lets a rule about to be added be compared against the ones already
    there, using the same code path as any two existing rules.
    """
    combinator = ANYOF if criteria.match == "any" else ALLOF

    tests = tuple(
        HeaderTest(term.header, criteria.compare, (term.value,))
        for term in criteria.terms
    )

    return Rule(
        index=index,
        name=name,
        combinator=combinator,
        tests=tests,
        actions=tuple(actions),
        stops=stops,
    )


# ############################################################################
# Containment
# ############################################################################


# ----------------------------------------------------------------------------
def _fold(value: str) -> str:
    """Case-fold a string the way ``i;ascii-casemap`` does, and no further.

    Every comparison in this section goes through here rather than through
    ``str.casefold()``; the reason that matters, and what breaks when it is
    "simplified" back, is on :data:`_CASEMAP`.
    """
    return value.translate(_CASEMAP)


# ----------------------------------------------------------------------------
def _comparable(test: HeaderTest) -> bool:
    """Decide whether this module is entitled to compare a test at all.

    Three separate ways a test falls outside what can be decided here. Each
    degrades a finding to POSSIBLE, which costs a reader a moment; deciding
    any of them anyway risks a CERTAIN the server disagrees with, which
    costs them mail.

    * **A match type outside** :data:`COMPARABLE` -- ``:matches`` is glob
      containment, a real problem this module does not attempt.
    * **A comparator other than the default.** ``i;octet`` compares bytes,
      so a test written with it matches strictly less than the same test
      without it, and the two must not reduce to the same thing. No
      semantics are invented for it or for any other comparator: the test
      is simply not compared.
    * **A key that is not pure ASCII.** ``i;ascii-casemap`` folds A-Z and
      compares every other octet as-is, so a non-ASCII key is decided by
      its exact bytes -- and the same text reaches us in more than one
      encoding of them. A header may arrive MIME-encoded and be decoded by
      the server (RFC 5228 has implementations compare in UTF-8), and
      Unicode gives one string several normal forms. So two non-ASCII keys
      that look equal are not reliably equal, and two that look unrelated
      are not reliably unrelated. Folding correctly is necessary but not
      sufficient here; the encoding question sits underneath it.
    """
    if test.match_type not in COMPARABLE:
        return False

    if test.comparator != ASCII_CASEMAP:
        return False

    return all(key.isascii() for key in test.keys)


# ----------------------------------------------------------------------------
def _key_covered(broad_keys, broad_match, narrow_key, narrow_match) -> bool:
    """Decide whether one key is always caught by a broader test.

    Reads as: any header value that satisfies ``narrow_key`` under
    ``narrow_match`` also satisfies at least one of ``broad_keys``.

    The three decidable combinations, and why the fourth is missing:

    * broad ``:contains`` vs narrow ``:contains`` -- a value containing the
      longer needle contains the shorter one, so substring containment of
      the keys settles it.
    * broad ``:contains`` vs narrow ``:is`` -- the value *is* the narrow
      key, so the same substring test applies.
    * broad ``:is`` vs narrow ``:is`` -- equality, case-insensitive to match
      Sieve's default ``i;ascii-casemap`` comparator.
    * broad ``:is`` vs narrow ``:contains`` is **not** decidable this way: a
      value merely containing the narrow key can be anything, so it need not
      equal the broad key. It falls through to False and is reported, if at
      all, as POSSIBLE.
    """
    needle = _fold(narrow_key)

    for key in broad_keys:
        candidate = _fold(key)

        if broad_match == "contains" and candidate in needle:
            return True

        if (
            broad_match == "is"
            and narrow_match == "is"
            and candidate == needle
        ):
            return True

    return False


# ----------------------------------------------------------------------------
def _test_covers(broad: HeaderTest, narrow: HeaderTest) -> bool:
    """Decide whether ``broad`` fires for every message ``narrow`` fires for.

    A Sieve ``header`` test with several keys succeeds if *any* key matches,
    so covering ``narrow`` means covering every one of its keys.
    """
    if _fold(broad.header) != _fold(narrow.header):
        return False

    if not _comparable(broad) or not _comparable(narrow):
        return False

    return all(
        _key_covered(broad.keys, broad.match_type, key, narrow.match_type)
        for key in narrow.keys
    )


# ----------------------------------------------------------------------------
def _disjunctive(rule: Rule) -> bool:
    """True when any single test firing is enough to fire the whole rule.

    An ``anyof`` qualifies by definition. So does an ``allof`` holding
    **one** test, and that case is not a nicety -- it is what the real
    scripts look like. Roundcube's filter UI writes a single-condition rule
    as ``allof (header ...)``, so treating every ``allof`` as undecidable
    made the analysis useless on exactly the scripts it exists to read: all
    four rules on the account it was first run against are one-test
    ``allof``s.

    ``allof`` and ``anyof`` over one test are the same predicate. Reading
    them as different is a fact about the parse tree, not about the mail.
    """
    return rule.combinator == ANYOF or len(rule.tests) == 1


# ----------------------------------------------------------------------------
def _covers(broad: Rule, narrow: Rule) -> str:
    """Return CERTAIN, POSSIBLE, or "" for whether broad subsumes narrow.

    Only one shape is decided outright, and it is the common one: a broad
    rule whose tests are ORed together, so that any single test firing is
    enough to fire the rule.

    * ``narrow`` is an ``anyof`` -- any of its tests can be what fires it,
      so **every** one has to be covered.
    * ``narrow`` is an ``allof`` -- all of its tests fire together, so
      covering **any one** of them is enough.

    A broad ``allof`` of **several** tests is not decided: it needs all of
    its own tests to fire, which depends on the message rather than on the
    narrow rule, so it can only ever be POSSIBLE.
    """
    if not broad.modelled or not narrow.modelled:
        return _possible(broad, narrow)

    if not _disjunctive(broad):
        return _possible(broad, narrow)

    covered = [
        any(_test_covers(wide, tight) for wide in broad.tests)
        for tight in narrow.tests
    ]

    if narrow.combinator == ALLOF:
        if any(covered):
            return CERTAIN

    elif all(covered):
        return CERTAIN

    return _possible(broad, narrow)


# ----------------------------------------------------------------------------
def _keys_overlap(
    left_key: str, left_match: str, right_key: str, right_match: str
) -> bool:
    """Decide whether one header value could satisfy both keys.

    Substring containment in **either** direction, which is the difference
    between a warning worth reading and noise. ``@lists.example.com`` and
    ``announce@lists.example.com`` overlap whichever is written first, so
    the pair is worth mentioning even when neither covers the other. Two
    unrelated addresses do not overlap at all.

    ``:is`` pins the whole value, so it can only overlap a key that fits
    inside it -- and two ``:is`` keys overlap only when they are equal.
    """
    left = _fold(left_key)
    right = _fold(right_key)

    if left_match == "is" and right_match == "is":
        return left == right

    if left_match == "is":
        return right in left

    if right_match == "is":
        return left in right

    return left in right or right in left


# ----------------------------------------------------------------------------
def _tests_overlap(left: HeaderTest, right: HeaderTest) -> bool:
    """Decide whether two tests could both fire on one message."""
    if _fold(left.header) != _fold(right.header):
        return False

    if not _comparable(left) or not _comparable(right):
        return False

    return any(
        _keys_overlap(left_key, left.match_type, right_key, right.match_type)
        for left_key in left.keys
        for right_key in right.keys
    )


# ----------------------------------------------------------------------------
def _possible(broad: Rule, narrow: Rule) -> str:
    """Return POSSIBLE when two rules could plausibly catch the same mail.

    Reached only after the decidable path has said "not certain", so this
    is asking a narrower question: is there real uncertainty here, or did
    we just prove these two have nothing to do with each other?

    An earlier draft warned whenever the two rules **shared a header**, on
    the reasoning that a dismissed warning is cheaper than a missed one.
    Running it against this account's real script disproved that: three of
    its four rules file distinct addresses out of ``To``, and every pair
    got flagged. A check that fires on three quarters of a correct rule set
    is not cautious, it is broken -- nobody reads the fourth warning.

    So the bar is *plausible overlap*, not a shared header:

    * either rule was not fully understood -- genuinely unknown;
    * a test on a shared header that :func:`_comparable` refuses -- a
      ``:matches`` glob, a non-default comparator, a non-ASCII key -- all
      undecidable here by design;
    * a broad ``allof`` -- it may or may not fire, depending on the message;
    * the keys actually overlap, one sitting inside the other.

    Two distinct addresses tested with ``:contains`` on the same header
    fall through all four and stay silent. They can still both match a
    message carrying both addresses -- a To line with two recipients -- but
    that is a different and much rarer thing than one rule shadowing
    another, and warning about it on every pair costs more than it finds.
    """
    if not broad.tests and not broad.unmodelled:
        return ""

    if not narrow.tests and not narrow.unmodelled:
        return ""

    if broad.unmodelled or narrow.unmodelled:
        return POSSIBLE

    shared = {_fold(test.header) for test in broad.tests} & {
        _fold(test.header) for test in narrow.tests
    }

    if not shared:
        return ""

    uncomparable = any(
        not _comparable(test)
        for test in (*broad.tests, *narrow.tests)
        if _fold(test.header) in shared
    )

    if uncomparable or not _disjunctive(broad):
        return POSSIBLE

    overlapping = any(
        _tests_overlap(wide, tight)
        for wide in broad.tests
        for tight in narrow.tests
    )

    return POSSIBLE if overlapping else ""


# ############################################################################
# Placement
# ############################################################################


# ----------------------------------------------------------------------------
def _reason(certainty: str, broad: Rule, narrow: Rule) -> str:
    """Phrase a finding, matching the wording to how sure it is."""
    if certainty == CERTAIN:
        return (
            f"{broad.name!r} carries stop and its conditions cover every "
            f"case {narrow.name!r} tests for"
        )

    if broad.unmodelled or narrow.unmodelled:
        return (
            f"{broad.name!r} carries stop and one of the two uses a "
            f"condition this check does not model, so overlap cannot be "
            f"ruled out"
        )

    return (
        f"{broad.name!r} carries stop and tests the same header, so it may "
        f"catch the same mail first"
    )


# ----------------------------------------------------------------------------
def analyze_placement(
    existing: list[Rule], candidate: Rule, at_index: int | None = None
) -> Analysis:
    """Report what inserting ``candidate`` at ``at_index`` would mean.

    ``at_index`` is the position the rule would occupy; ``None`` means the
    end, which is what ``add`` does today. Rules before it can stop it from
    ever running; rules after it can be stopped by it.

    Only a rule carrying ``stop`` can shadow anything, because without it
    evaluation continues to the next rule regardless.
    """
    position = len(existing) if at_index is None else max(0, at_index)

    analysis = Analysis()

    for rule in existing[:position]:
        if not rule.stops:
            continue

        certainty = _covers(rule, candidate)

        if certainty:
            analysis.dead_on_arrival.append(
                Shadow(
                    certainty=certainty,
                    broad=rule.name,
                    narrow=candidate.name,
                    reason=_reason(certainty, rule, candidate),
                )
            )

    if candidate.stops:
        for rule in existing[position:]:
            certainty = _covers(candidate, rule)

            if certainty:
                analysis.starves.append(
                    Shadow(
                        certainty=certainty,
                        broad=candidate.name,
                        narrow=rule.name,
                        reason=_reason(certainty, candidate, rule),
                    )
                )

    return analysis


# ----------------------------------------------------------------------------
def audit(rules: list[Rule]) -> list[Shadow]:
    """Report rules the script already contains that cannot fire.

    The same comparison as :func:`analyze_placement`, turned on the script
    itself: every rule is checked against the ones that precede it. A rule
    that was appended months ago and has been dead ever since looks
    identical, from the outside, to one that simply never matched any mail.
    """
    findings = []

    for index, entry in enumerate(rules):
        analysis = analyze_placement(rules[:index], entry, at_index=index)

        findings.extend(analysis.dead_on_arrival)

    return findings

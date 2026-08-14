"""The shared criteria model, and the two translations it drives.

One ``Criteria`` object produces the Sieve conditions for mail that has not
arrived and the IMAP SEARCH key for mail that already has. If those two
drift, ``mxfilter add`` files new mail one way and old mail another, and
nothing in the output says so -- which is why the agreement tests below
assert both renderings of the *same* object rather than testing each side
on its own.
"""

import pytest

from mxfilter import MxFilterError
from mxfilter.criteria import (
    COMPARE_OPS,
    MATCH_MODES,
    Criteria,
    Term,
    escape_sieve_string,
    longest_literal,
    sieve_pattern_to_regex,
)

# ############################################################################
# Construction
# ############################################################################


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("given", "canonical"),
    [
        pytest.param("from", "From", id="from"),
        pytest.param("FROM", "From", id="upper"),
        pytest.param("  list-id  ", "List-Id", id="padded"),
        pytest.param("reply-to", "Reply-To", id="hyphenated"),
        pytest.param("X-Custom", "X-Custom", id="unknown-kept-as-typed"),
    ],
)
def test_add_canonicalizes_the_header_name(given, canonical):
    """Canonical spellings keep the generated script and dry-run tidy.

    Both Sieve and IMAP treat header names case-insensitively, so this is
    presentation -- but it is presentation the user reads back in a diff
    before approving an upload.
    """
    criteria = Criteria()
    criteria.add(given, "value")

    assert criteria.terms[0].header == canonical


# ----------------------------------------------------------------------------
def test_add_refuses_an_empty_value():
    """An empty value would widen the rule to every message with a header."""
    criteria = Criteria()

    with pytest.raises(MxFilterError, match="has an empty value"):
        criteria.add("from", "")


# ----------------------------------------------------------------------------
def test_a_term_refuses_an_empty_header():
    with pytest.raises(MxFilterError, match="needs a header name"):
        Term("", "value")


# ----------------------------------------------------------------------------
def test_an_unknown_match_mode_is_refused():
    with pytest.raises(MxFilterError, match=r"--match must be one of"):
        Criteria(match="either")


# ----------------------------------------------------------------------------
def test_an_unknown_compare_op_is_refused():
    with pytest.raises(MxFilterError, match=r"--compare must be one of"):
        Criteria(compare="regex")


# ----------------------------------------------------------------------------
def test_no_criteria_is_refused_before_anything_is_generated():
    """A rule with no conditions matches every message in the mailbox.

    Both renderings guard this, because either one reaching a server
    unguarded is a mailbox-wide action nobody asked for.
    """
    criteria = Criteria()

    assert not criteria

    with pytest.raises(MxFilterError, match="no criteria given"):
        criteria.require_terms()

    with pytest.raises(MxFilterError, match="no criteria given"):
        criteria.sieve_conditions()

    with pytest.raises(MxFilterError, match="no criteria given"):
        criteria.imap_search_key()


# ----------------------------------------------------------------------------
def test_describe_names_the_comparison_and_the_joiner():
    criteria = Criteria(match="all", compare="is")
    criteria.add("from", "a@example.com")
    criteria.add("subject", "Report")

    described = criteria.describe()

    assert " AND " in described
    assert "From is 'a@example.com'" in described
    assert "Subject is 'Report'" in described


# ----------------------------------------------------------------------------
def test_describe_uses_or_for_match_any():
    criteria = Criteria(match="any")
    criteria.add("from", "a@example.com")
    criteria.add("to", "b@example.com")

    assert " OR " in criteria.describe()


# ############################################################################
# Sieve rendering
# ############################################################################


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mode", "matchtype"),
    [
        pytest.param("any", "anyof", id="any"),
        pytest.param("all", "allof", id="all"),
    ],
)
def test_sieve_matchtype_maps_the_match_mode(mode, matchtype):
    assert Criteria(match=mode).sieve_matchtype() == matchtype


# ----------------------------------------------------------------------------
@pytest.mark.parametrize("compare", COMPARE_OPS)
def test_sieve_conditions_carry_the_comparator_tag(compare):
    criteria = Criteria(compare=compare)
    criteria.add("subject", "Report")

    assert criteria.sieve_conditions() == [
        ("Subject", f":{compare}", "Report")
    ]


# ----------------------------------------------------------------------------
def test_sieve_conditions_escape_every_value():
    """Escaping at render time is what keeps a flag or a quote intact."""
    criteria = Criteria()
    criteria.add("subject", 'a "quote" and a \\ backslash')

    ((_header, _tag, value),) = criteria.sieve_conditions()

    assert value == escape_sieve_string('a "quote" and a \\ backslash')
    assert value == 'a \\"quote\\" and a \\\\ backslash'


# ############################################################################
# IMAP rendering
# ############################################################################


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("header", "key"),
    [
        pytest.param("from", ["FROM", "x"], id="from-shortcut"),
        pytest.param("to", ["TO", "x"], id="to-shortcut"),
        pytest.param("cc", ["CC", "x"], id="cc-shortcut"),
        pytest.param("bcc", ["BCC", "x"], id="bcc-shortcut"),
        pytest.param("subject", ["SUBJECT", "x"], id="subject-shortcut"),
        pytest.param("list-id", ["HEADER", "List-Id", "x"], id="header-form"),
        pytest.param("x-spam", ["HEADER", "x-spam", "x"], id="custom-header"),
    ],
)
def test_imap_uses_the_first_class_key_where_one_exists(header, key):
    """IMAP has dedicated keys for a few headers; they are cheaper.

    The HEADER fallback has to stay correct for everything else, since the
    headers people actually filter on (List-Id above all) have no
    shortcut.
    """
    criteria = Criteria()
    criteria.add(header, "x")

    assert criteria.imap_search_key() == [key]


# ----------------------------------------------------------------------------
def test_match_all_becomes_imaps_implicit_and():
    criteria = Criteria(match="all")
    criteria.add("from", "a")
    criteria.add("subject", "b")

    assert criteria.imap_search_key() == [["FROM", "a"], ["SUBJECT", "b"]]


# ----------------------------------------------------------------------------
def test_match_any_becomes_a_right_nested_or_chain():
    """IMAP's OR is binary, so three terms need nesting, not a flat list.

    A flat ``OR a b c`` is a syntax error the server rejects; getting this
    wrong fails loudly, but getting the *nesting* wrong (left-associative)
    would silently change which messages match.
    """
    criteria = Criteria(match="any")
    criteria.add("from", "a")
    criteria.add("to", "b")
    criteria.add("cc", "c")

    assert criteria.imap_search_key() == [
        ["OR", ["FROM", "a"], ["OR", ["TO", "b"], ["CC", "c"]]]
    ]


# ----------------------------------------------------------------------------
def test_a_single_term_needs_no_or_wrapper():
    criteria = Criteria(match="any")
    criteria.add("from", "a")

    assert criteria.imap_search_key() == [["FROM", "a"]]


# ----------------------------------------------------------------------------
def test_extra_search_keys_are_anded_on_the_end():
    criteria = Criteria(match="any")
    criteria.add("from", "a")

    key = criteria.imap_search_key(extra=["UNSEEN", "NOT", "DELETED"])

    assert key == [["FROM", "a"], "UNSEEN", "NOT", "DELETED"]


# ----------------------------------------------------------------------------
def test_matches_reduces_a_glob_to_its_longest_literal():
    """The search key for a glob must be broad, never narrow.

    IMAP cannot glob, so the derived substring has to be one every real
    match necessarily contains -- otherwise the retroactive pass silently
    skips mail the Sieve rule will catch.
    """
    criteria = Criteria(compare="matches")
    criteria.add("subject", "*[ALERT]*production*")

    assert criteria.imap_search_key() == [["SUBJECT", "production"]]


# ----------------------------------------------------------------------------
def test_a_glob_of_pure_wildcards_degrades_to_a_bare_header_test():
    """Correct-but-broad beats narrow: the post-filter narrows it back."""
    criteria = Criteria(compare="matches")
    criteria.add("list-id", "*")

    assert criteria.imap_search_key() == [["HEADER", "List-Id", ""]]


# ----------------------------------------------------------------------------
def test_header_names_are_distinct_and_keep_their_spelling():
    criteria = Criteria()
    criteria.add("from", "a")
    criteria.add("FROM", "b")
    criteria.add("subject", "c")

    assert criteria.header_names() == ["From", "Subject"]


# ############################################################################
# Sieve and IMAP agree
# ############################################################################


# ----------------------------------------------------------------------------
@pytest.mark.parametrize("match", MATCH_MODES)
@pytest.mark.parametrize("compare", COMPARE_OPS)
def test_one_object_drives_both_renderings(match, compare):
    """The agreement check, across every match x compare combination.

    Sieve gets the exact comparator; IMAP gets a key that is equal or
    broader and is then re-checked. Both come from this one object, which
    is the whole reason the two halves of ``add`` cannot disagree.
    """
    criteria = Criteria(match=match, compare=compare)
    criteria.add("from", "boss@example.com")
    criteria.add("subject", "Report")

    conditions = criteria.sieve_conditions()
    key = criteria.imap_search_key()

    assert [term[0] for term in conditions] == ["From", "Subject"]
    assert {term[1] for term in conditions} == {f":{compare}"}

    if match == "all":
        assert key == [["FROM", "boss@example.com"], ["SUBJECT", "Report"]]
        assert criteria.sieve_matchtype() == "allof"

    else:
        assert key == [
            ["OR", ["FROM", "boss@example.com"], ["SUBJECT", "Report"]]
        ]
        assert criteria.sieve_matchtype() == "anyof"


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("match", "headers", "expected"),
    [
        pytest.param(
            "any",
            {"FROM": ["boss@example.com"]},
            True,
            id="any-first-term-only",
        ),
        pytest.param(
            "any", {"SUBJECT": ["Report"]}, True, id="any-second-term-only"
        ),
        pytest.param(
            "any", {"FROM": ["someone@else.com"]}, False, id="any-neither"
        ),
        pytest.param(
            "all", {"FROM": ["boss@example.com"]}, False, id="all-needs-both"
        ),
        pytest.param(
            "all",
            {"FROM": ["boss@example.com"], "SUBJECT": ["Report"]},
            True,
            id="all-both-present",
        ),
    ],
)
def test_the_post_filter_honours_the_match_mode(match, headers, expected):
    criteria = Criteria(match=match, compare="contains")
    criteria.add("from", "boss@example.com")
    criteria.add("subject", "Report")

    assert criteria.matches(headers) is expected


# ############################################################################
# The exact re-check
# ############################################################################


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("compare", "value", "header", "expected"),
    [
        pytest.param(
            "contains",
            "report",
            "Weekly Report",
            True,
            id="contains-substring",
        ),
        pytest.param(
            "contains",
            "REPORT",
            "Weekly Report",
            True,
            id="contains-is-case-insensitive",
        ),
        pytest.param(
            "contains",
            "quarterly",
            "Weekly Report",
            False,
            id="contains-absent",
        ),
        pytest.param(
            "is", "Weekly Report", "Weekly Report", True, id="is-exact"
        ),
        pytest.param(
            "is",
            "weekly report",
            "Weekly Report",
            True,
            id="is-is-case-insensitive",
        ),
        pytest.param(
            "is", "Report", "Weekly Report", False, id="is-rejects-a-substring"
        ),
        pytest.param(
            "is",
            "Weekly Report",
            "  Weekly Report  ",
            True,
            id="is-ignores-surrounding-space",
        ),
        pytest.param(
            "matches",
            "Weekly*",
            "Weekly Report",
            True,
            id="matches-trailing-star",
        ),
        pytest.param(
            "matches",
            "*Report",
            "Weekly Report",
            True,
            id="matches-leading-star",
        ),
        pytest.param(
            "matches",
            "Weekly",
            "Weekly Report",
            False,
            id="matches-needs-the-whole-value",
        ),
        pytest.param(
            "matches",
            "Weekl? Report",
            "Weekly Report",
            True,
            id="matches-single-char-wildcard",
        ),
        pytest.param(
            "matches",
            "Weekl? Report",
            "Weekly  Report",
            False,
            id="matches-question-mark-is-exactly-one",
        ),
    ],
)
def test_each_comparator_means_what_sieve_means(
    compare, value, header, expected
):
    """IMAP SEARCH is substring-only, so this re-check is the real test.

    ``is`` and ``matches`` are both narrower than the search that found the
    candidate. Without this pass the retroactive run would move mail the
    Sieve rule will never touch -- the two halves disagreeing in the one
    direction the user cannot see.
    """
    criteria = Criteria(compare=compare)
    criteria.add("subject", value)

    assert criteria.matches({"SUBJECT": [header]}) is expected


# ----------------------------------------------------------------------------
def test_matches_tests_the_whole_header_value_not_the_address():
    """The case the implementer hit; pinned so nobody 'fixes' it wrong.

    Sieve's ``:matches`` compares the *entire* header value, and a real
    From header carries a display name. So ``*@lists.example.com`` does not
    match ``Announce <announce@lists.example.com>`` -- and it must not,
    because the Sieve rule on the server will not match it either. Making
    this pass would put the retroactive run out of step with the filter it
    was generated alongside.
    """
    criteria = Criteria(compare="matches")
    criteria.add("from", "*@lists.example.com")

    bare = {"FROM": ["announce@lists.example.com"]}
    with_display_name = {"FROM": ["Announce <announce@lists.example.com>"]}

    assert criteria.matches(bare) is True
    assert criteria.matches(with_display_name) is False

    # The glob a user wanting both would have to write.
    forgiving = Criteria(compare="matches")
    forgiving.add("from", "*@lists.example.com*")

    assert forgiving.matches(with_display_name) is True


# ----------------------------------------------------------------------------
def test_a_repeated_header_matches_on_any_occurrence():
    """Received and Delivered-To appear many times in one message."""
    criteria = Criteria(compare="is")
    criteria.add("delivered-to", "me@example.com")

    headers = {"DELIVERED-TO": ["other@example.com", "me@example.com"]}

    assert criteria.matches(headers) is True


# ----------------------------------------------------------------------------
def test_a_missing_header_simply_does_not_match():
    """A message without the header is a normal case, not an error."""
    criteria = Criteria(compare="contains")
    criteria.add("list-id", "github.com")

    assert criteria.matches({}) is False
    assert criteria.matches({"LIST-ID": []}) is False


# ############################################################################
# Glob translation
# ############################################################################


# ----------------------------------------------------------------------------
def test_a_bracket_is_a_literal_not_a_character_class():
    """Sieve globs have exactly ``*`` and ``?`` -- fnmatch would add more.

    Subjects with brackets are ordinary (``[ALERT]``, ``[PATCH]``), so
    treating ``[...]`` as a character class would silently stop matching
    the very subjects people write globs for.
    """
    pattern = sieve_pattern_to_regex("[ALERT]*")

    assert pattern.match("[ALERT] disk full")
    assert not pattern.match("A disk full")


# ----------------------------------------------------------------------------
def test_a_backslash_escapes_a_wildcard():
    pattern = sieve_pattern_to_regex(r"100\* off")

    assert pattern.match("100* off")
    assert not pattern.match("100 percent off")


# ----------------------------------------------------------------------------
def test_a_glob_spans_a_newline():
    """Header values are unfolded, but a stray newline must not truncate."""
    pattern = sieve_pattern_to_regex("start*end")

    assert pattern.match("start\nend")


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("pattern", "literal"),
    [
        pytest.param("*production*", "production", id="between-stars"),
        pytest.param("a*bcdef*gh", "bcdef", id="longest-of-three"),
        pytest.param("plain", "plain", id="no-wildcards"),
        pytest.param("*", "", id="only-a-wildcard"),
        pytest.param(r"esc\*aped", "esc*aped", id="escaped-star-is-literal"),
    ],
)
def test_longest_literal_picks_the_longest_wildcard_free_run(pattern, literal):
    assert longest_literal(pattern) == literal

"""The actions mxfilter will not emit, and why each one is refused.

Two different reasons, deliberately kept apart, and the difference is the
whole point of these tests:

* ``redirect`` is **confirmed disabled** by MXroute (their 2024-03-21
  announcement), so the refusal names the alternative that actually
  works -- a forwarder.
* ``notify`` and ``vacation`` are simply **not implemented here**. No
  MXroute source says either way, so the message must not claim they are
  unavailable; it points at the control panel and at ``mxfilter test``,
  which reads the answer off the server.

Collapsing those two into one "unsupported" message would state as fact
something this project has explicitly recorded as unconfirmed
(CONVENTIONS.md > Confidence).

The refusal has to happen at generation time. Emitting the action instead
produces a script the server rejects -- or, worse, one it accepts and that
silently drops mail.
"""

import pytest

from mxfilter import MxFilterError
from mxfilter.cli import build_parser, reject_forbidden, sieve_actions
from mxfilter.sieve import MXROUTE_FORBIDDEN_ACTIONS, UNIMPLEMENTED_ACTIONS

# ############################################################################
# Helpers
# ############################################################################


# ----------------------------------------------------------------------------
def parse_add(*extra: str):
    """Parse a valid ``add`` invocation plus whatever ``extra`` adds."""
    return build_parser().parse_args(
        ["add", "--from", "boss@example.com", "--fileinto", "Lists", *extra]
    )


# ############################################################################
# Refusals
# ############################################################################


# ----------------------------------------------------------------------------
def test_redirect_is_refused_and_names_the_forwarder_alternative():
    """The one refusal resting on a documented MXroute limitation.

    Naming the alternative matters more here than in the other cases: the
    user wants forwarding, forwarding exists on MXroute, and it lives
    somewhere else entirely (the panel, or the sibling Terraform
    provider's ``mxroute_forwarder`` resource). A bare refusal would send
    them looking for a Sieve workaround that cannot exist.
    """
    with pytest.raises(MxFilterError) as caught:
        reject_forbidden(parse_add("--redirect", "elsewhere@example.com"))

    message = str(caught.value)

    assert "disables the Sieve 'redirect' action" in message
    assert "forwarder" in message
    assert "mxroute_forwarder" in message
    assert "SRS" in message


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("flag", "value", "label"),
    [
        pytest.param(
            "--notify",
            "mailto:me@example.com",
            "notify (enotify)",
            id="notify",
        ),
        pytest.param(
            "--vacation", "back on Monday", "vacation", id="vacation"
        ),
    ],
)
def test_an_unimplemented_action_is_refused_without_claiming_it_is_disabled(
    flag, value, label
):
    """Refused because we do not generate it -- not because it is blocked.

    The message has to say that plainly and hand the user the way to find
    out for themselves, since nothing MXroute publishes answers it.
    """
    with pytest.raises(MxFilterError) as caught:
        reject_forbidden(parse_add(flag, value))

    message = str(caught.value)

    assert f"does not generate the Sieve '{label}' action" in message
    assert "conservative choice of ours" in message
    assert "not a documented" in message
    assert "mxfilter test" in message


# ----------------------------------------------------------------------------
def test_the_two_refusal_sets_do_not_overlap():
    """A confirmed limitation and an unimplemented action are different.

    If a name ever appears in both, one of the two messages is a lie about
    what MXroute actually does.
    """
    overlap = set(MXROUTE_FORBIDDEN_ACTIONS) & set(UNIMPLEMENTED_ACTIONS)

    assert overlap == set()


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name", sorted({*MXROUTE_FORBIDDEN_ACTIONS, *UNIMPLEMENTED_ACTIONS})
)
def test_every_refused_action_has_a_flag_the_gate_can_see(name):
    """The gate reads ``args`` by attribute, so a missing flag is a hole.

    Without the (hidden) flag the user's request would fall through to
    argparse's own 'unrecognized arguments' error, which explains nothing
    about why mxfilter will not do it.
    """
    args = parse_add(f"--{name}", "value")

    assert getattr(args, name) == "value"

    with pytest.raises(MxFilterError):
        reject_forbidden(args)


# ----------------------------------------------------------------------------
def test_an_ordinary_add_passes_the_gate():
    """The failure path is only meaningful if the success path is clear."""
    assert reject_forbidden(parse_add()) is None


# ############################################################################
# Nothing forbidden ever reaches the generated script
# ############################################################################

REFUSED_VERBS = {*MXROUTE_FORBIDDEN_ACTIONS, *UNIMPLEMENTED_ACTIONS}


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "extra",
    [
        pytest.param([], id="fileinto-only"),
        pytest.param(["--mark-read"], id="with-a-flag"),
        pytest.param(["--keep"], id="with-keep"),
        pytest.param(["--discard"], id="discard"),
        pytest.param(["--flag", "\\Flagged", "--no-stop"], id="flag-no-stop"),
    ],
)
def test_the_generated_actions_never_contain_a_refused_verb(extra):
    """Belt and braces behind the gate: the generator has no such branch."""
    actions = sieve_actions(parse_add(*extra), "INBOX.Lists", False)
    verbs = {action[0] for action in actions}

    assert verbs.isdisjoint(REFUSED_VERBS)


# ----------------------------------------------------------------------------
def test_an_add_with_no_action_at_all_is_refused():
    """A rule that tests but does nothing is never what someone meant."""
    args = build_parser().parse_args(["add", "--from", "boss@example.com"])

    with pytest.raises(MxFilterError, match="no action requested"):
        sieve_actions(args, "", False)


# ----------------------------------------------------------------------------
def test_flags_are_emitted_before_fileinto_and_stop_comes_last():
    """Order decides whether the delivered copy carries the flag.

    ``addflag`` after ``fileinto`` flags a message that has already been
    filed, and a rule without a trailing ``stop`` lets a later rule move
    the same mail again.
    """
    args = parse_add("--mark-read")
    actions = sieve_actions(args, "INBOX.Lists", False)

    assert [action[0] for action in actions] == ["addflag", "fileinto", "stop"]
    assert actions[0] == ("addflag", "\\\\Seen")

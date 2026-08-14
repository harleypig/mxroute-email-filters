"""The gate for the live tier. Skipping is the default and the fallback.

These tests talk to a **real MXroute account**. Every path out of this
file that is not an explicit, fully-configured opt-in is a skip -- an
unset variable, a mistyped one, a missing credential, and any error
working out whether the gate is open all end the same way.

The gate is deliberately narrow:

* ``MXFILTER_LIVE`` must equal exactly ``"1"``. Not "set", not truthy --
  ``true``, ``yes`` and ``0`` all skip. ``make testlive`` sets it to
  ``1``; nothing else should be doing so by accident.
* The account settings must all be present. A half-configured run would
  otherwise fail against *some* server, which is worse than not running.

TESTS.md holds the rest of the safety contract for anything added here.
The two clauses a future writing test must satisfy, neither of which the
read-only smoke tests below need:

* **Back up the active script before writing and restore it afterwards,
  including on failure.** The account's real filters are not the test's
  to lose.
* **Never move real mail in INBOX.** Use a purpose-made folder, prefer
  messages the test appended itself, and tear it down.
"""

import os

import pytest

LIVE_FLAG = "MXFILTER_LIVE"
LIVE_VALUE = "1"

REQUIRED_SETTINGS = ("MXROUTE_HOST", "MXROUTE_USER")
PASSWORD_SETTINGS = ("MXROUTE_PASSWORD", "MXROUTE_PASSWORD_CMD")


# ############################################################################
# The gate
# ############################################################################


# ----------------------------------------------------------------------------
def gate_reason() -> str | None:
    """Return why the live tier must not run, or None if it may.

    Written as "why not" rather than "may we" on purpose: a new condition
    is added by returning a reason, so forgetting to update the caller
    cannot turn into an accidental live run.
    """
    if os.environ.get(LIVE_FLAG) != LIVE_VALUE:
        return (
            f"live tier is off: set {LIVE_FLAG}={LIVE_VALUE} (or run "
            f"'make testlive') to hit a real MXroute account"
        )

    missing = [name for name in REQUIRED_SETTINGS if not os.environ.get(name)]

    if missing:
        return f"live tier needs {', '.join(missing)} in the environment"

    if not any(os.environ.get(name) for name in PASSWORD_SETTINGS):
        return (
            f"live tier needs a credential: set one of "
            f"{' or '.join(PASSWORD_SETTINGS)} "
            f"({PASSWORD_SETTINGS[1]} is preferred -- it keeps the value "
            f"out of the environment)"
        )

    return None


# ----------------------------------------------------------------------------
def pytest_runtest_setup(item):
    """Skip every test in this directory unless the gate is fully open.

    This runs for each item under ``tests/live/`` regardless of its
    markers, so a live test whose author forgot ``@pytest.mark.live`` is
    still gated.
    """
    reason = gate_reason()

    if reason is not None:
        pytest.skip(reason)


# ############################################################################
# Fixtures
# ############################################################################


# ----------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def isolated_environment():
    """Override the offline tier's environment scrubbing.

    ``tests/conftest.py`` clears every ``MXROUTE_*`` variable so the unit
    tests cannot depend on the developer's account. The live tier is the
    one place those variables are the input, so the scrubbing is
    disabled here rather than worked around inside each test.
    """
    return None


# ----------------------------------------------------------------------------
@pytest.fixture(scope="session")
def live_config():
    """The account settings, resolved the same way the CLI resolves them."""
    import argparse

    from mxfilter.config import load_config

    return load_config(argparse.Namespace())


# ----------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def no_network():
    """Override the offline tier's socket block.

    ``tests/conftest.py`` makes ``socket.socket`` raise so a unit test
    cannot reach a server by accident. Reaching a server is this tier's
    entire job, so the block is lifted here -- behind the gate above, and
    nowhere else.
    """
    return None

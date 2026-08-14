"""The core returns data; only the CLI prints.

``config``, ``criteria``, ``sieve``, and ``imap`` return structured values
and raise ``MxFilterError``. Every piece of rendering, prompting, and
progress output lives in ``cli.py`` (CONVENTIONS.md).

That split is worth a test rather than a convention alone for two reasons,
and the second is the sharp one:

* A core that prints cannot be tested without capturing stdout, and a
  second front-end would have to tear it apart before reusing it.
* ``config`` holds the mailbox password. A ``print`` or a ``getpass``
  added to it later is precisely how a credential reaches a transcript --
  the failure this repo treats as a hard boundary. A convention prevents
  that only until someone is in a hurry; this test prevents it every run.

The check is an AST walk rather than a grep, so the word "print" appearing
in a docstring -- which it does, in ``config.Secret`` -- is not a finding,
and a call written as ``builtins.print`` still is.
"""

import ast
from pathlib import Path

import pytest

import mxfilter

# Anything that puts a value in front of a person, or takes one from them.
BANNED_NAMES = {"print", "input", "breakpoint"}

# getpass is the CLI's to own: a core module that prompts owns a terminal,
# which is what stops a non-terminal front-end reusing it (config.password
# takes a `prompter` callback for exactly this reason).
BANNED_ATTRIBUTES = {"getpass", "getpass_", "print_exc"}

CORE_MODULES = ("config", "criteria", "sieve", "imap")

# ############################################################################
# Helpers
# ############################################################################


# ----------------------------------------------------------------------------
def module_path(name: str) -> Path:
    """Return the source file of one mxfilter module."""
    return Path(mxfilter.__file__).parent / f"{name}.py"


# ----------------------------------------------------------------------------
def called_names(tree: ast.AST) -> list[str]:
    """Return the name of every function called anywhere in ``tree``.

    A bare call reports its identifier (``print``); a method or module
    call reports the attribute (``sys.stdout.write`` -> ``write``). That
    is deliberately blunt: over-reporting is a conversation, while
    under-reporting is a credential in a log.
    """
    names = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        function = node.func

        if isinstance(function, ast.Name):
            names.append(function.id)

        elif isinstance(function, ast.Attribute):
            names.append(function.attr)

    return names


# ############################################################################
# The guard
# ############################################################################


# ----------------------------------------------------------------------------
@pytest.mark.parametrize("name", CORE_MODULES)
def test_a_core_module_never_prints_or_prompts(name):
    source = module_path(name).read_text(encoding="utf-8")
    called = set(called_names(ast.parse(source)))

    found = called & (BANNED_NAMES | BANNED_ATTRIBUTES)

    assert found == set(), (
        f"mxfilter/{name}.py calls {sorted(found)}; presentation and "
        f"prompting belong in cli.py (CONVENTIONS.md > The core returns "
        f"data)"
    )


# ----------------------------------------------------------------------------
@pytest.mark.parametrize("name", CORE_MODULES)
def test_a_core_module_never_imports_a_prompting_module(name):
    """Importing ``getpass`` at all signals the split has been crossed."""
    source = module_path(name).read_text(encoding="utf-8")
    imported = set()

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert "getpass" not in imported


# ----------------------------------------------------------------------------
@pytest.mark.parametrize("name", CORE_MODULES)
def test_a_core_module_never_exits_the_process(name):
    """Libraries raise; only an executable may exit (code-style.md).

    ``MxFilterError`` exists so ``cli.main`` can turn a failure into one
    diagnostic line and a status code. A ``sys.exit`` in the core takes
    that decision away from every caller, including a future front-end.
    """
    source = module_path(name).read_text(encoding="utf-8")
    called = set(called_names(ast.parse(source)))

    assert "exit" not in called
    assert "_exit" not in called


# ----------------------------------------------------------------------------
def test_the_guard_would_actually_catch_a_violation():
    """A guard nobody has seen fail is a guard nobody should trust."""
    tree = ast.parse("import sys\ndef f(secret):\n    print(secret)\n")

    assert "print" in called_names(tree)


# ----------------------------------------------------------------------------
def test_reveal_is_called_only_where_a_credential_is_handed_to_a_client():
    """Every real disclosure site, enumerated -- the reviewable list.

    ``Secret.reveal()`` is greppable precisely so this list can exist. A
    new call site is not necessarily wrong, but it is always a decision
    somebody should have made on purpose rather than in passing.
    """
    sites = {}

    for name in CORE_MODULES:
        source = module_path(name).read_text(encoding="utf-8")
        count = called_names(ast.parse(source)).count("reveal")

        if count:
            sites[name] = count

    assert sites == {"sieve": 1, "imap": 1}, (
        f"Secret.reveal() call sites changed: {sites}. Each one hands the "
        f"password to a connection method and nothing else; review the "
        f"new one against CONVENTIONS.md > Credentials."
    )

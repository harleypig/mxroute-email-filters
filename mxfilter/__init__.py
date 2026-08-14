"""Manage MXRoute email filters over ManageSieve and IMAP.

``mxfilter`` builds a Sieve rule from command-line criteria, merges it
non-destructively into the account's active script, and optionally applies
the same criteria to mail that has already been delivered.

The package is deliberately split so the offline logic (criteria
translation, Sieve generation, folder-name normalization) can be exercised
without a server:

``config``    credential and endpoint resolution
``criteria``  the shared criteria model, to Sieve *and* to IMAP SEARCH
``sieve``     the ManageSieve client wrapper and script merge helpers
``imap``      the IMAP client wrapper (folders, search, move, flag)
``cli``       argument parsing and the subcommand implementations
"""

__all__ = ["MxFilterError", "__version__"]

__version__ = "0.1.0"


# ############################################################################
# Errors
# ############################################################################


class MxFilterError(Exception):
    """An expected failure that should be reported without a traceback.

    Every code path that can fail for a reason the user can act on raises
    this (or a subclass) with a message written for a human. ``cli.main``
    turns it into a one-line diagnostic and a non-zero exit; the raw
    traceback is only shown under ``--debug``.
    """

# mxfilter — Agent Guide

Auto-loaded entry point for AI agents working in `mxroute-email-filters` — a
Python CLI (`mxfilter`) that manages MXroute email filters end to end: it
writes server-side Sieve rules over ManageSieve, and applies the same rule
retroactively to mail already in the mailbox over IMAP. Repo conventions and
the test layout are pulled in via the imports at the bottom.

## The few things to internalize first

- **The tool has two halves, and the second one is the point.** Sieve only
  ever affects **new incoming** mail. The retroactive IMAP pass over mail
  already delivered is why this exists — a change that writes a Sieve rule and
  stops there is half-done.
- **Credentials are a hard boundary.** The mailbox password must **never**
  reach stdout, stderr, a log, or a transcript. `config.Secret` renders
  `<redacted>` from both `__str__` and `__repr__`; only `reveal()` returns the
  value, and it is called **only** when handing the password to a connection
  method. Never print, format, or log it — including in a throwaway debug shim
  (the global `CLAUDE.md` *Secret Handling* holds test doubles to the same
  bar).
- **The language is Python ≥ 3.11.** The global `python.md` / `ruff.md` apply;
  lint **and** format are `ruff` (never black/isort/flake8 alongside it).
- **Discover from the server; never hardcode.** Capabilities, the active
  script name, and the folder delimiter are read at runtime — see
  [CONVENTIONS.md](CONVENTIONS.md) › *Discover, don't hardcode*. MXroute is
  mid-migration on several of these.
- **MXroute disables the Sieve `redirect` action.** Forwarding is the
  substitute, and it is **already code** in the sibling repo
  (`mxroute_forwarder` in `terraform-provider-mxroute`) — point at it rather
  than reimplementing it here.
- **The MXroute REST API has no filter/Sieve endpoints**, which is *why* this
  is a standalone CLI and not a resource in that provider (see
  [ADR 0001](../adr/0001-standalone-cli-over-provider-resource.md)).
- **The merge into the active script is non-destructive, deliberately.**
  Roundcube's own filter UI writes the same script; overwriting it would
  silently destroy hand-made filters (see
  [ADR 0002](../adr/0002-non-destructive-script-merge.md)).

## Where the rest lives

Repo conventions in [CONVENTIONS.md](CONVENTIONS.md); test layout in
[TESTS.md](TESTS.md); decisions already made in [../adr/](../adr/README.md);
dev basics in [../README.md](../README.md). Generic agent behavior — git/gh
workflow, code style, the Python toolchain, the QA dimensions — comes from the
maintainer's global `~/.claude/` config, which this repo defers to except
where these files override it.

@CONVENTIONS.md
@TESTS.md

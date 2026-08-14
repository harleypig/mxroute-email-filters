# mxfilter Test Layout

The global `testing.md` carries the bar (success **and** failure paths, a
regression test per bug, a manual verification note per feature); `python.md`
carries the layout convention (`tests/` at the repo root, mirroring the
package). This file records what belongs here.

**The offline tier is written and green** — 347 passing, 3 live-gated skips.
The live tier is scaffolded (`tests/live/`) and skipped by default; it stays
open until it has run against a real account, and the backup-and-restore
fixture required before anything writes to one is still outstanding
([#9](https://github.com/harleypig/mxroute-email-filters/issues/9)).

## Two tiers

1. **Unit tests** (`tests/test_*.py`) — **offline**, no network, no
   credentials. The default gate, and the tier that should hold almost
   everything. The package was split specifically so this tier can reach the
   interesting logic:
   - `criteria` — a criteria set translated to Sieve **and** to IMAP `SEARCH`,
     including the cases where the two differ and the client-side re-check
     closes the gap.
   - `sieve` — parse an existing script, merge a rule into it, render it back,
     and confirm rules the tool did not write survive verbatim. The
     overwrite-would-have-destroyed-it case is the one that matters
     ([ADR 0002](../adr/0002-non-destructive-script-merge.md)); it deserves a
     test with a hand-written Roundcube-shaped script as its fixture.
   - `sieve` — a refused action (`redirect`, `notify`, `vacation`) raises with
     the pointer message rather than emitting the action.
   - `imap` — folder-name normalization across both separators
     (`Lists/GitHub` ≡ `INBOX.Lists.GitHub`) against a reported delimiter.
   - `config` — the flag → env → file → default resolution order, and that a
     `Secret` renders `<redacted>` from `str()`, `repr()`, and an f-string.
2. **Live tests** (`MXFILTER_LIVE=1`) — stand up **real** Sieve scripts and
   move **real** mail against a **live MXroute account**. They mutate real
   state; run them manually (`make testlive`), **never** in a default gate.

The live tier is also this repo's **end-to-end** pass (`qa.md` dimension 8) —
there is no third tier and no separate e2e suite. A CLI that talks to two
servers has no meaningful integration layer between "offline logic" and "does
it actually work against MXroute".

## Live-test credentials & safety

Live tests touch a real mailbox, so the guards are not optional:

- **Credentials come from the environment** — the `MXROUTE_*` variables
  (`config.py`), with the password via `MXROUTE_PASSWORD_CMD` in preference to
  `MXROUTE_PASSWORD`. `MXFILTER_LIVE=1` is required, so a plain `pytest` can
  never touch the account.
- **The password stays out of every artifact.** A live test's output, a
  captured log, and a failure traceback are all places a credential could
  surface — `Secret` is what prevents it, so a live test must never unwrap a
  password to build a fixture or a diagnostic. See CONVENTIONS.md ›
  *Credentials*.
- **Back up and restore the active script.** A live run must capture the
  account's existing script before it writes, and put it back afterwards —
  including on failure. The account's real filters are not the test's to lose.
- **Scope the mail-moving tests to a dedicated folder.** They must not run
  against `INBOX` and must not disturb live mail; use a purpose-made test
  folder and tear it down. Prefer messages the test appended itself over
  whatever happens to be in the mailbox.
- **Assume nothing about the server's configuration.** The live tier is
  precisely where *Discover, don't hardcode* (CONVENTIONS.md) gets exercised —
  a test that hardcodes the delimiter, the script name, or `INBOX.spam` is
  testing our assumption rather than the server.

## Manual verification

Every shipped feature also carries a plain-language note — where to go, what
to do, what success looks like **concretely**, and what failure looks like —
written in the PR as the first draft of the user-facing docs
(`testing.md` › *The manual verification bar*). This matters more than usual
here: a filter that was written but silently does nothing looks identical, at
the terminal, to one that works. "The rule was added" is not a success
criterion; "`mxfilter list` shows the rule, and a new message matching it
lands in `Lists/GitHub`" is.

## Running

```sh
pytest                 # unit (offline, credential-free)
make test              # the same, via the Makefile
make testlive          # live (MXFILTER_LIVE=1; needs MXROUTE_* in the env)
```

`TESTARGS` passes extra flags through, e.g. a run filter for a scoped live
pass: `make testlive TESTARGS='-k sieve'`.

# TODO

## Testing

- [x] Land the offline unit tier (`tests/`) — 222 passing, shape per
  [`.claude/TESTS.md`](.claude/TESTS.md). The Roundcube-script case was
  written first and immediately earned its keep: it found that `sievelib`
  drops Roundcube's `# rule:[name]` markers, renaming every webmail-authored
  filter to `Unnamed rule N` on the first merge.
- [ ] Add the backup-and-restore fixture the live tier requires before any
  test is allowed to write to a real account
  ([`tests/live/conftest.py`](tests/live/conftest.py) names it as the
  prerequisite). Until it exists, the live tier stays read-only.
- [ ] Exercise the whole flow against a **real MXroute account** (the live
  tier, `MXFILTER_LIVE=1`). This is also what settles the unconfirmed protocol
  facts in [`.claude/CONVENTIONS.md`](.claude/CONVENTIONS.md) › *Confidence* —
  the ManageSieve port and its TLS mode, whether `notify` and `vacation` are
  actually disabled, the folder delimiter, the spam folder's real name, and
  which script name Roundcube's managesieve plugin writes. **Record each
  answer back into that section** as it lands, moving it up a confidence tier.

## Tooling

- [ ] Wire `pyright` (`pyright.md`). Still **Planned** in the QA status
  table; the workflow it would slot into now exists, so this is a job to
  add rather than a pipeline to build.
- [ ] Revisit the merge sentinels now that CI gates `master`
  ([`.claude/CONVENTIONS.md`](.claude/CONVENTIONS.md) › *Merge policy*).
  Their stated precondition — required status checks that actually run — is
  now met.

## Features & fixes

- [ ] **A `restore` subcommand.** `mxfilter backup` now makes the copy and
  nothing puts it back, so the gap is conspicuous rather than merely noted
  ([README.md](README.md) › *Safety*, [docs/VERIFYING.md](docs/VERIFYING.md) ›
  *If something looks wrong*). It was left out of the backup change
  deliberately: it is a **write path against a live account** — the one that
  replaces a script wholesale rather than merging into it — so it needs its
  own confirmation flow (show the diff between the file and what is on the
  server, then ask), its own tests, and a decision about whether it may
  restore over a script whose current contents mxfilter cannot parse. Until
  then the answer is another ManageSieve client, such as `sieve-connect`.

- [ ] Expand `~` in a password-file path. `password_file = "~/pw"` in
  `config.toml` currently fails with "No such file" naming the literal
  `~/pw`, which reads as a missing file rather than an unexpanded path.
  `Path(value).expanduser()` is the whole fix; it was left out of the
  password-sources change as unrequested scope.

- [ ] **Free-standing comments are dropped on merge.** `sievelib`'s `tosieve`
  re-emits only its name and description markers, so a user's own
  `# this one is for the accountant` disappears the first time mxfilter
  merges into the script. No rule is lost — bodies and names both survive
  ([ADR 0002](adr/0002-non-destructive-script-merge.md) › *What "survives"
  means*) — but the user's intent is, and it goes the quiet way: discovered
  days later, with nothing connecting it to a command that reported success.
  The fix has a known shape: the lexer-token rewriter added for the
  `# rule:[...]` translation (`sieve._rewrite_hash_comments`) already
  identifies comment tokens precisely, so the work is capturing the
  free-standing ones with their anchor position and re-emitting them, not
  finding them.

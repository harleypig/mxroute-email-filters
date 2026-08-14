## Unreleased

Nothing has been released yet — `mxfilter` is pre-`v0.1.0` and is not
published anywhere (see [RELEASING.md](RELEASING.md)). Entries accumulate here
under the usual headings — `BREAKING CHANGES:`, `FEATURES:`, `ENHANCEMENTS:`,
`BUG FIXES:`, `NOTES:` — and move under a `## X.Y.Z` heading when a tag is
cut.

FEATURES:

* **Sieve rules over ManageSieve.** Build a rule from criteria flags
  (`--from`, `--to`, `--cc`, `--subject`, `--list-id`, `--header NAME=VALUE`,
  with `--match any|all` and `--compare contains|is|matches`), merge it into
  the account's **active** script, validate it server-side with `CHECKSCRIPT`,
  and activate it. Subcommands: `add`, `from-message`, `apply`, `remove-rule`,
  `list`, `show`, `folders`, `test`.
* **The retroactive pass over mail already delivered.** Sieve only ever runs
  on new incoming mail, so the same criteria are translated to IMAP `SEARCH`
  and applied to messages already in the mailbox — `MOVE` where the server
  advertises it, `COPY` + `\Deleted` + `EXPUNGE` where it does not. One
  criteria model drives both outputs, which is what keeps the Sieve rule and
  the retroactive pass in agreement.
* **Non-destructive merging.** The existing active script is parsed and merged
  into, never overwritten, so filters written in webmail survive. A parse
  failure is a hard stop, never a fall-back to overwrite
  ([ADR 0002](adr/0002-non-destructive-script-merge.md)).
* **Roundcube rule names are preserved.** Both `# rule:[NAME]` (Roundcube's
  managesieve plugin) and `# Filter: NAME` (`sievelib`) are read, and
  Roundcube's dialect is written — so the webmail and this tool co-edit one
  script instead of mangling each other's names.
* **Runtime discovery.** Sieve capabilities, the active script name, and the
  folder hierarchy delimiter are read from the server rather than hardcoded,
  so `Lists/News` and `INBOX.Lists.News` resolve to whichever spelling the
  account actually uses.

NOTES:

* **Nothing here has run against a live MXroute account.** The 246-test suite
  is entirely offline, and the live tier (`tests/live/`) is gated behind
  `MXFILTER_LIVE` and has never been exercised.
  [docs/VERIFYING.md](docs/VERIFYING.md) is the ordered first-run procedure,
  from commands that touch nothing up to ones that move mail.
* **Safety defaults.** `--dry-run` on every mutating command; the active
  script is backed up before every upload; the retroactive pass always
  previews and confirms; `--max-messages` (default 500) refuses an
  over-cap batch outright rather than processing a partial set that would
  read as complete.
* **Credentials never reach output.** There is deliberately no `--password`
  flag — a password given as an argument is visible in the process list and
  shell history. Use `MXROUTE_PASSWORD_CMD`, the environment, or the
  interactive prompt.
* **MXroute refusals are tiered by confidence.** `redirect` is refused
  because MXroute documents disabling it; `notify` and `vacation` are refused
  as a conservative choice of ours, and say so rather than implying a
  documented restriction.
* CI runs `ruff check`, `ruff format --check`, and `pytest` on every pull
  request and on pushes to `master`; both checks are required by the branch
  ruleset.

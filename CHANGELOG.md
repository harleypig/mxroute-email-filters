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

ENHANCEMENTS:

* **`mxfilter backup` — save the active script on demand.** Fetches the active
  script and writes it to a file, changing nothing on the server. The file is
  the server's **exact bytes**: no banner lines, nothing reformatted, no
  newline translated. That distinction is the point of the command —
  `mxfilter show` decorates its output with `# ---- name ----` and
  `# ---- N rule(s): ...` for a reader, so redirecting `show` to a file
  produces something that looks like a backup and cannot be restored, which is
  what [docs/VERIFYING.md](docs/VERIFYING.md) step 3 used to tell people to
  do. `--output PATH` overrides the location: a PATH ending in `/`, or naming
  a directory that already exists, means "write the default filename in here";
  anything else is the exact file to write. Written mode `0600` in a directory
  created `0700` — a Sieve script is not a credential, but it does say who the
  user corresponds with and how they sort it. `--dry-run` reports the file it
  would write and writes nothing.
* **Backups now default to the config directory** — usually
  `~/.config/mxfilter/backups`, or `$XDG_CONFIG_HOME/mxfilter/backups` — in
  place of `$XDG_STATE_HOME/mxfilter/backups`. XDG would call a backup *state*
  rather than config, and this is a deliberate departure from that rather than
  an XDG-endorsed reading: a backup the user cannot find is not a backup, and
  the config directory is the one mxfilter path they already know, having put
  `config.toml` in it. The automatic pre-upload backup and `mxfilter backup`
  share the one location, so there is no second directory to look in.
  `--backup-dir` and `MXROUTE_BACKUP_DIR` override it exactly as before. **Any
  backups already under `~/.local/state/mxfilter/backups` stay there** —
  nothing moves them.
* **Still no restore command.** The file is the server's exact bytes, and
  putting one back needs another ManageSieve client (`sieve-connect`, or a
  panel filter UI that exposes a raw import). `backup --help` says so, and a
  `restore` subcommand is tracked in [TODO.md](TODO.md): it is a write path
  against a live account and deserves its own confirmation flow and tests.
* **A password file source.** `--password-file PATH`,
  `MXROUTE_PASSWORD_FILE`, and `password_file` in the config file read the
  credential from a file, which — unlike an environment variable — can be
  closed to everyone but its owner. mxfilter **refuses** to read a file any
  group or other bit is set on (so `0600` and `0400` pass, `0644` does not),
  naming the path, the mode, and the `chmod` that fixes it, and never opening
  the file — the rule `libpq` applies to `~/.pgpass`, but said out loud
  rather than by silently ignoring the file. On WSL the file must live on
  the Linux filesystem, since a Windows mount reports `0777` whatever it is
  set to. Exactly one trailing newline is stripped and nothing else is — a
  trailing space can be part of a password. `mxfilter test` reports
  `set (via file)`.
* **`--password VALUE` (`-p`), the least safe source.** Added by request, and
  warned about on stderr the way `mysql` does: an argument is visible in the
  process list to every user on the machine, and the shell has already
  written it to history. Prefer `--password-file` or `MXROUTE_PASSWORD_FILE`.
  This supersedes the earlier note that there is deliberately no
  `--password` flag.
* **The three credential flags are mutually exclusive.** `--password-file`,
  `--password-cmd`, and `--password` are three equally explicit instructions
  with no natural ranking, so argparse rejects a second one rather than
  picking a winner the user would have to have memorised.

BUG FIXES:

* **The password resolution order was inverted for flags.**
  `MXROUTE_PASSWORD` beat an explicit `--password-cmd`, contradicting the
  `CLI flag > environment > config file > default` rule the rest of the
  settings follow. The hazard was concrete: with `MXROUTE_PASSWORD` exported
  for one account, running `--password-cmd` against a **different** account
  authenticated as the first one — the wrong mailbox, silently, with no error
  anywhere. The order is now, highest first: an explicit flag →
  `MXROUTE_PASSWORD_FILE` → `MXROUTE_PASSWORD_CMD` → `MXROUTE_PASSWORD` →
  `password_file` → `password_cmd` (config file) → the interactive prompt. A
  flag typed for this run beats an ambient variable, and a literal value
  never beats an instruction about where to fetch one.

NOTES:

* **Nothing here has run against a live MXroute account.** The 347-test suite
  is entirely offline, and the live tier (`tests/live/`) is gated behind
  `MXFILTER_LIVE` and has never been exercised.
  [docs/VERIFYING.md](docs/VERIFYING.md) is the ordered first-run procedure,
  from commands that touch nothing up to ones that move mail.
* **Safety defaults.** `--dry-run` on every mutating command; the active
  script is backed up before every upload; the retroactive pass always
  previews and confirms; `--max-messages` (default 500) refuses an
  over-cap batch outright rather than processing a partial set that would
  read as complete.
* **Credentials never reach output.** The password is wrapped in a `Secret`
  that renders `<redacted>` from both `__str__` and `__repr__`, so no
  `print`, f-string, `%s`, or traceback frame can disclose it; `reveal()` is
  the only way out and its call sites are pinned by a test. `--password`
  exists but is the least safe source and warns for the reason above —
  prefer `--password-file`, `MXROUTE_PASSWORD_FILE`, or
  `MXROUTE_PASSWORD_CMD`.
* **MXroute refusals are tiered by confidence.** `redirect` is refused
  because MXroute documents disabling it; `notify` and `vacation` are refused
  as a conservative choice of ours, and say so rather than implying a
  documented restriction.
* CI runs `ruff check`, `ruff format --check`, and `pytest` on every pull
  request and on pushes to `master`; both checks are required by the branch
  ruleset.

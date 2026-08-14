# mxfilter

Manage MXRoute email filters from the command line: build a Sieve rule from
criteria flags, merge it into the account's **active** script without
disturbing the rules already there, and apply the same criteria to mail that
has already been delivered.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Configure

Copy `.env.example` and fill it in, or write
`$XDG_CONFIG_HOME/mxfilter/config.toml`:

```toml
host = "mail.example-server.mxrouteXX.com"
user = "you@yourdomain.com"
password_cmd = "pass show email/you@yourdomain.com"
default_folder = "Lists"
```

Resolution order is **CLI flag > environment > config file > default**.

The password has more sources than the other settings, so it has its own
ladder — the same shape, highest first:

| # | Source |
|---|--------|
| 1 | `--password-file`, `--password-cmd`, or `--password` (mutually exclusive) |
| 2 | `MXROUTE_PASSWORD_FILE` |
| 3 | `MXROUTE_PASSWORD_CMD` |
| 4 | `MXROUTE_PASSWORD` |
| 5 | `password_file` in the config file |
| 6 | `password_cmd` in the config file |
| 7 | an interactive prompt |

A flag typed for this run beats a variable that merely happens to be
exported, and a literal value never beats an instruction about where to
fetch one. The config file still never holds the password itself — only
`password_file` or `password_cmd`.

`--password-file` is refused unless the file's mode is `0600` or `0400`; the
error names the `chmod` that fixes it. Keep that file on the Linux
filesystem — anything under `/mnt/c` or another Windows mount (WSL) reports
mode `0777` whatever you set, so it is always refused. Exactly one trailing
newline is stripped from it, and nothing else, since a trailing space can be
part of a password.

`--password VALUE` exists and is the **least safe** option: the value is
visible in the process list to every user on the machine and your shell
saves it to history. mxfilter warns when you use it.

## Use

```bash
# Check both services and what they support. Changes nothing.
mxfilter test

# What does this server call its folders?
mxfilter folders

# See exactly what would change, without changing it.
mxfilter add --from newsletter@example.com --fileinto Lists/News --dry-run

# Do it: merge the rule, upload, activate, then file existing mail.
mxfilter add --from newsletter@example.com --fileinto Lists/News

# Rule only; leave delivered mail alone.
mxfilter add --list-id python-list.python.org --fileinto Lists/Python \
    --no-apply

# Learn the criteria from a message you already have.
mxfilter from-message --folder INBOX --search 'FROM newsletter@example.com' \
    --fileinto Lists/News --dry-run

# Existing mail only; no Sieve change. --create-folder because the target
# may not exist yet.
mxfilter apply --subject '[SPAM]' --fileinto Quarantine --create-folder \
    --mark-read

mxfilter list
mxfilter show
mxfilter remove-rule from-newsletter-example-com

# Save the active script, byte for byte, before you touch anything.
mxfilter backup
mxfilter backup --output ~/mxfilter-before-first-run.sieve
```

**Before the first run against a real mailbox, work through
[docs/VERIFYING.md][verify].** It is an ordered ladder from commands that
touch nothing to ones that move mail, and it is where the unconfirmed
assumptions below get settled for your account.

## Safety

* `--dry-run` changes nothing, on every mutating command. It prints whatever
  that command would have changed: the Sieve diff for `add`, `from-message`,
  and `remove-rule`; the list of matching messages for `add`, `from-message`,
  and `apply`; the file that would have been written for `backup`.
* The current active script is backed up to a timestamped file before any
  upload, and the path is printed. `mxfilter backup` takes the same copy on
  demand, without changing anything on the server.
* Backups land in `$XDG_CONFIG_HOME/mxfilter/backups` (usually
  `~/.config/mxfilter/backups`) — beside your `config.toml`, one file per
  backup, named `<script>-<UTC timestamp>.sieve`. XDG would call a backup
  *state* rather than config; keeping it here is a deliberate departure from
  that, not something XDG endorses, because a backup you cannot find is not a
  backup. `--backup-dir` and `MXROUTE_BACKUP_DIR` move it. The file is written
  mode `0600` in a directory created `0700`: a Sieve script is not a password,
  but it does say who you correspond with and how you sort it.
* **mxfilter has no restore command.** The backup is the server's exact bytes
  — no banner lines, nothing reformatted — and putting them back needs another
  ManageSieve client, such as `sieve-connect`, or the panel's filter UI if it
  exposes a raw import. See [docs/VERIFYING.md][verify].
* Rules are merged into the parsed existing script, never appended blindly,
  so other rules survive. If the existing script cannot be parsed, mxfilter
  stops rather than overwrite it.
* `checkscript` runs on the server before `putscript`.
* The existing-mail pass **always previews and always confirms** before it
  touches anything — `--dry-run` shortens that path, it is not what creates
  it. `--yes` skips the prompts. Deletion says in as many words that it
  cannot be undone; a move says it can be reversed.
* `--max-messages` (default 500) refuses the whole batch when more matches
  than that come back. It never processes a partial set: silent truncation
  reads as "it handled everything" when it did not.
* A `--fileinto` target that does not exist is a **warning, not an error**,
  unless you pass `--create-folder`. `add` will still write the rule, and
  mail filed there by the server later may be lost. `apply` refuses outright,
  since it would have nowhere to put the messages.
* `--create-folder` also **subscribes** to the folder it creates. Existing
  and visible are different questions on IMAP: webmail draws its folder tree
  from the subscription list (`LSUB`), not from the folder list (`LIST`), so
  a folder that is created but never subscribed to receives mail and never
  appears. `--no-subscribe` skips the subscription on purpose — somewhere to
  file a high-volume list that should leave the inbox without cluttering the
  sidebar — and mxfilter says so on the line where it creates the folder,
  because an invisible folder nobody was told about is the bug, not the
  feature. If the subscription fails, the folder is **not** torn back down:
  it exists and mail filed there will arrive, so mxfilter warns and tells
  you to subscribe to it from your mail client.

## MXRoute specifics

MXRoute documents very little of its Sieve surface, so these are grouped by
how much is actually known. Nothing here is promoted a tier to make the
documentation read better.

### Confirmed

* The `redirect` action is **disabled server-side** — MXRoute announced this
  on 2024-03-21, saying their own forwarders "are designed to properly handle
  SRS" where a Sieve redirect does not. `--redirect` fails with a pointer to
  the panel's Forwarders (or the `mxroute_forwarder` Terraform resource).
* The username is the **full email address**, on both IMAP and ManageSieve.
* The hostname is **per-account** — the same as your primary MX record, shown
  on the panel's Email Clients page. There is deliberately no default.
* IMAP is **993** (implicit TLS) or **143** (STARTTLS). mxfilter picks the
  mode from the port: anything other than 143 is treated as implicit TLS.
* MXRoute's REST API exposes nothing for filters or Sieve, which is why this
  tool speaks ManageSieve rather than an API.

### Likely, not confirmed

* The folder delimiter is probably `.` (Maildir++, so `INBOX.Lists.News`),
  and the spam folder is probably `INBOX.spam` — **lowercase**, not
  `INBOX.Junk`. The only evidence either way is an incidental filesystem path
  in an MXRoute blog post, not a documented statement, and that post's
  subject is that the "deliver spam to spam folder" option was **removed**,
  so the folder may not exist on your account at all.
* Because of that, neither is assumed. The delimiter is **detected at
  runtime** from the server's folder list, and folder names are matched
  against that list case-insensitively — type `Lists/News` or
  `INBOX.Lists.News` and whichever spelling the server reports is used for
  both the Sieve rule and the move. `mxfilter folders` is the authority for
  your account. The one exception is `--no-imap`, which has no folder list to
  consult and falls back to `.` (or `--delimiter`), and warns that it did.

### Unconfirmed — do not read these as MXRoute facts

* **The ManageSieve port and TLS mode.** MXRoute documents neither. The
  defaults here (4190, STARTTLS) are the IANA/RFC 5804 registered port and
  the Dovecot default — that is why they are the defaults, and it is not a
  verified MXRoute setting. `--sieve-port` and `--sieve-tls` exist because of
  that, and a connection failure says so rather than implying you mistyped.
* **Whether `notify` and `vacation` are disabled.** No MXRoute source says
  either way. mxfilter refuses both as **our own conservative default**, not
  as a documented MXRoute limitation, and its error says so and points at the
  control panel. `mxfilter test` prints what your server actually advertises.
* **ManageSieve script-size, script-count, and rate limits.** Unknown; there
  is no documented ceiling to design against.

The active script's name is read from `LISTSCRIPTS` and written back to, and
is never guessed — the webmail's script name is server-side config, and
MXRoute is mid-migration on both its panel and Dovecot. `--script` overrides
it; the name `mxfilter` is used only when the account has no scripts at all.

## `--compare` tests the whole header value

`--compare contains` (the default) is a substring test and behaves the way
you would expect. `--compare is` and `--compare matches` do not: both compare
against the **entire** header value, because that is what Sieve's `:is` and
`:matches` do.

The trap is that a `From` header is rarely just an address. Given:

```text
From: Announce <announce@lists.example.com>
```

the whole value is `Announce <announce@lists.example.com>`, so:

* `--compare matches --from '*@lists.example.com'` does **not** match. The
  pattern is anchored at both ends, and the value does not end at the
  address — there is still a `>` after it.
* `--compare matches --from '*@lists.example.com*'` **does** match. The
  trailing `*` absorbs the `>`.
* `--compare is --from 'announce@lists.example.com'` does **not** match
  either, for the same reason.
* `--from 'announce@lists.example.com'` — the default `contains` — matches,
  and is usually what you actually wanted.

The wildcards in a `matches` pattern are exactly `*` (any run of characters)
and `?` (any single character), with `\` escaping either. `[...]` is not a
character class, so a bracketed subject is safe.

## Known limits

* `is` and `matches` cannot be expressed in IMAP SEARCH, which only does
  case-insensitive substring matching. So the existing-mail pass searches
  deliberately too broadly and then re-checks every candidate's real headers
  in Python against the Sieve semantics above. Correct, but it fetches the
  header block of every candidate the broad search returned, not just the
  ones that end up matching.
* Merging round-trips the script through a parser. Rules that have no
  `# Filter:` name comment are renamed `Unnamed rule N`, and formatting is
  normalized. The diff shows this before anything is uploaded.
* Nothing here evaluates the Sieve script you already have. mxfilter can
  apply criteria you give it to old mail; it cannot tell you which of your
  existing rules would have caught a message.

[verify]: docs/VERIFYING.md

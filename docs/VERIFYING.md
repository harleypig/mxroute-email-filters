# Verifying mxfilter against a real mailbox

**Nothing below has ever been run against a live MXRoute server.** Every part
of mxfilter is tested offline, against fixtures. This is a **first-run
procedure**, not a regression checklist: you are finding out whether it works,
not confirming that it still does.

Work through the steps **in order**. They are arranged from *touches nothing*
to *cannot be undone*, so you can stop at any point and have changed nothing
you did not mean to.

Two things to hold on to the whole way down:

* **Moving mail is recoverable. Expunging is not.** A move leaves the message
  intact in another folder, and you can move it back. `--discard` expunges it
  from the server, and there is nothing to undo.
* **Use a scratch folder for first runs.** Not Trash — a client or the server
  may empty Trash on a schedule, which turns a reversible move into a
  permanent deletion without asking you.

## Before you start

Set `host`, `user`, and a password source, per [Configure][config] in the
README. Have your webmail open in a browser tab; several steps ask you to
confirm something there.

## 1. `mxfilter test` — touches nothing

```bash
mxfilter test
```

This connects to both services, reads what they advertise, and exits. It
writes nothing anywhere.

**You should see**, in this order:

* Five config lines: `Host`, `User`, `Password`, and the two endpoints.
  `Password:` reads `set`, `set (via command)`, or `unset`. `unset` is fine —
  it means you will be prompted.
* `ManageSieve: connected`, then a list of advertised extensions.
* A table of named extensions with `yes` or `not advertised` beside each.
  **`fileinto` and `imap4flags` must say `yes`** — those are what an ordinary
  rule needs. `mailbox` saying `yes` means Sieve can create the target folder
  itself.
* `active script:` followed by a name, or `(none)`. Write the name down; that
  is the script mxfilter will edit.
* `IMAP: connected`, then the delimiter, the folder count, `MOVE`, `UIDPLUS`,
  and `FILTER=SIEVE`.
* A closing note about `redirect`.

**Expect `FILTER=SIEVE: no`.** That is the Dovecot plugin that would let the
server apply a Sieve script to old mail itself. It is experimental and off by
default, so its absence is normal and nothing is wrong. mxfilter does its own
client-side pass either way and has no code path that uses it.

**Note what `MOVE` says.** `MOVE: yes` means moves are atomic. `MOVE: no
(COPY+EXPUNGE)` means mxfilter copies, marks deleted, and expunges as three
steps; if that sequence breaks partway you get duplicates, not lost mail, but
it is worth knowing before step 6.

**Stop if:**

* You never reach `ManageSieve: connected`. ManageSieve is tried first, so a
  failure there means IMAP was never tested at all. The error names the port
  and TLS mode it used and says plainly that MXRoute documents neither. Try
  `mxfilter test --sieve-tls ssl`, then `--sieve-port` with something else,
  then ask MXRoute support.
* Authentication fails on either service. The username must be the **full
  email address**, not the part before the `@`.
* `fileinto` says `not advertised`. Do not continue; a rule that files mail
  is the whole point, and the server would reject the script.

## 2. `mxfilter folders` — read-only

```bash
mxfilter folders
```

**You should see** `Hierarchy delimiter: '<char>'`, a folder count, and every
folder name, one per line, exactly as the server spells them.

**This is the step that settles the guesses.** The README lists two things as
*likely but unconfirmed*, and this output is what makes them facts for your
account. Check both:

* **Is the delimiter `.`?** If the listing looks like `INBOX.Lists.News`, yes.
  If it looks like `Lists/News`, your server is not Maildir++ and the README's
  folder examples need reading with that substitution.
* **Is there an `INBOX.spam`, and is it lowercase?** It may be `INBOX.Spam`,
  `INBOX.Junk`, or absent entirely — MXRoute removed the option that created
  it, so a newer account may never have had one. **Whatever appears here is
  the truth for your account.** Copy the spelling exactly if you ever file
  into it.

**Stop if** the folder count is 1 or the list is missing folders you can see
in webmail. Something is wrong with the account or the connection, and every
later step depends on this list being complete.

## 3. `mxfilter list` and `mxfilter show` — read-only, and save a copy

**Do this before anything that writes.** Save the current active script:

```bash
mxfilter list
mxfilter show > ~/mxfilter-before-first-run.sieve
```

**You should see** `mxfilter list` print one line per script, with `*` and
`(active)` marking the active one. Then check the saved file:

```bash
cat ~/mxfilter-before-first-run.sieve
```

**You should see** your existing filters as Sieve source, wrapped in two
banner lines that `show` adds: `# ---- <name> ----` at the top and
`# ---- N rule(s): ...` at the bottom. Those are comments, not rules — strip
them if you ever feed this file back to a server.

**If you have filters in Roundcube, this file is them.** Roundcube's filter UI
writes the same active script mxfilter is about to edit. This copy is what
protects them.

**Stop if:**

* `mxfilter list` prints `No Sieve scripts on the server.` That is not a
  failure — it means you have no filters yet, there is nothing to lose, and
  mxfilter will create a script called `mxfilter` on first upload. Skip the
  `show` and carry on.
* The saved file is empty but `list` showed an active script. Do not continue;
  something is wrong with the download and you have no backup.

## 4. `--dry-run` on a real rule — changes nothing

Pick a real sender you actually get mail from, and a folder that already
exists (use the exact spelling from step 2).

```bash
mxfilter add --from newsletter@example.com --fileinto Lists/News --dry-run
```

**You should see**, in this order: a folder-resolution line if the name you
typed had to be respelled, a plain-English summary of the rule (`when:` /
`then:`), a unified diff of the script, the line `[dry-run] the script was NOT
uploaded.`, and then the existing-mail preview.

**Scrutinize the diff, line by line. This is the important part.**

* **Every rule you recognise must still be there.** A `-` line removing a
  `# Filter: <name>` for a rule you did not name is a **stop**. The merge
  should only ever add.
* **Reformatting is expected.** mxfilter parses the script and re-renders it,
  so indentation, quoting, and line breaks may all change. That is normal.
* **A rule renamed to `Unnamed rule N` is expected**, for any existing rule
  that had no `# Filter:` name comment. The rule itself is unchanged; only its
  label is invented. Check the conditions and actions on those lines match
  what was there before.
* **The `require` line may gain entries** such as `fileinto` or `imap4flags`.
  That is mxfilter keeping the header correct for the union of all rules.

**Then scrutinize the message list.**

* It prints `N message(s) match:` and up to 20 of them, with uid, date,
  sender, and subject.
* **Is this the mail you expected?** Read the senders and subjects. One
  message in that list you would be unhappy to see moved means the criteria
  are wrong, not that the tool is.
* The last line says what a real run would do — `[dry-run] would move N
  message(s) to '<folder>'`.

**Stop if:**

* The diff says `(no change)`. Either the rule already exists or the criteria
  produced nothing; find out which before re-running without `--dry-run`.
* You get `a rule named '<name>' already exists`. Pick a different `--name`,
  or pass `--replace` if you genuinely mean to overwrite it.
* A warning says the target folder does not exist. `add` will still write the
  rule, and mail the server files there later may be lost. Add
  `--create-folder`, or fix the folder name against step 2.
* The match count is far larger than you expected. Fix the criteria. If you
  used `--compare is` or `--compare matches`, read [the whole-header
  section][compare] in the README — those two compare against the entire
  header value, which is almost never just the address.

## 5. First real `add`, with `--no-apply` — Sieve only, no mail moved

Same command as step 4, with `--dry-run` swapped for `--no-apply`:

```bash
mxfilter add --from newsletter@example.com --fileinto Lists/News --no-apply
```

This uploads the rule and touches **no existing mail**. Sieve applies only to
messages that arrive from now on.

**You should see** the same summary and diff as step 4, then three new lines:

* `Backed up current script to <path>` — note the path.
* `Uploaded and activated script '<name>'`.
* `Skipping the existing-mail pass (--no-apply).`

**Verify, in three places:**

1. The backup file exists and is not empty:

   ```bash
   ls -l <the path it printed>
   ```

   By default backups land in `~/.local/state/mxfilter/backups`, one file per
   upload, named `<script>-<UTC timestamp>.sieve`.

2. The server has your rule, and still has the others:

   ```bash
   mxfilter show
   ```

   The last line reads `# ---- N rule(s): <names>`. **Your new rule name must
   appear there, and so must every name that was in the file you saved in
   step 3.**

3. **Open Roundcube's filter UI.** Your old filters and the new one should
   both be listed. This is the check that matters most — it is where a lost
   filter would actually show up, and it is what step 3's backup exists to
   fix.

Then wait for a matching message to arrive and confirm it lands in the target
folder rather than INBOX.

**Stop if:**

* `the server rejected the generated script (CHECKSCRIPT ...)`. Nothing was
  uploaded and nothing was changed — the check runs before the upload. If a
  warning about unadvertised extensions preceded it, that is your cause.
* `mxfilter show` is missing a rule that was in your step 3 file, or Roundcube
  shows fewer filters than before. Do not run anything else. Go to
  [If something looks wrong](#if-something-looks-wrong).

## 6. Retroactive apply, deliberately narrow, into a scratch folder

Now the existing-mail pass — the only part that moves real messages.

**Make the criteria match a handful of messages, not a category.** Pick a
subject string so specific you can name the messages it will hit before you
run it. Send them to a scratch folder, never Trash, and never with
`--discard`.

```bash
mxfilter apply --subject 'Your invoice for March' --fileinto Scratch \
    --create-folder --max-messages 5 --dry-run
```

Run it with `--dry-run` first and read the message list. Then run it for real
by dropping that flag:

```bash
mxfilter apply --subject 'Your invoice for March' --fileinto Scratch \
    --create-folder --max-messages 5
```

`--max-messages 5` is a deliberate safety belt: if more than five messages
match, mxfilter refuses the **whole** batch and touches nothing, rather than
processing part of it. Being stopped here is a success, not a failure — it
means the criteria were broader than you thought.

**You should see:**

* `Created IMAP folder 'INBOX.Scratch'` (spelled per your server's delimiter).
* `Criteria: Subject contains 'Your invoice for March'`.
* `Searching 'INBOX' for existing matches...` then `N message(s) match:` and
  the preview.
* A prompt: `Move N message(s) from 'INBOX' to 'INBOX.Scratch'? [y/N]`.
  Answer `y`.
* `Moved N message(s) from 'INBOX' to 'INBOX.Scratch'`.

**Verify in your mail client:** the scratch folder holds exactly those N
messages, your INBOX is down by exactly N, and the messages themselves are
intact — open one and check the body is there, not just the headers.

**To reverse it**, move them back from your mail client, or:

```bash
mxfilter apply --folder INBOX.Scratch --subject 'Your invoice for March' \
    --fileinto INBOX
```

`--folder` names the *source* and, unlike `--fileinto`, is passed to the
server exactly as you type it. Use the spelling mxfilter printed when it
created the folder, not `Scratch`.

**Stop if:**

* The count moved does not match the count previewed.
* Any message is missing from both folders. Say so before running anything
  else.

**Do not use `--discard` to try any of this.** It permanently deletes, the
prompt says so in as many words, and there is no undo.

## 7. Real use

Only now. Two habits worth keeping:

* Run `--dry-run` first on anything whose match count you cannot predict.
* Set `--max-messages` to roughly what you expect, rather than leaving it at
  the 500 default. Being refused costs you one re-run; not being refused can
  cost you a mis-sorted mailbox.

## If something looks wrong

**If you just added a rule you did not want**, remove it by name:

```bash
mxfilter remove-rule <rule-name> --dry-run
mxfilter remove-rule <rule-name>
```

Read the diff before confirming; the same merge round-trip applies.

**If the whole script looks wrong** — rules missing, or mangled — the backup
mxfilter printed in step 5 is the server's exact previous bytes, before that
upload. So is the copy you saved in step 3.

**mxfilter has no restore command.** It can only ever merge into whatever is
currently on the server. To put a backup back you need another ManageSieve
client: a command-line one such as `sieve-connect`, or Roundcube's filter UI
if the panel exposes its raw filter-set edit or import view — whether it does
is a server-side setting nobody has checked here. Once it is restored, run
`mxfilter show` and compare against your saved file before doing anything
else.

If the backup and the current script differ in ways you did not expect, that
is worth reporting with both files in hand — they are the whole evidence of
what happened.

[compare]: ../README.md#--compare-tests-the-whole-header-value
[config]: ../README.md#configure

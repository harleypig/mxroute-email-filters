# 2. Merge into the active Sieve script; never overwrite it

- Status: accepted
- Date: 2026-08-13

## Context

An MXroute account already has an active Sieve script, and **Roundcube's
managesieve plugin writes to that same script** — it is the file the panel's
filter UI edits. Meanwhile ManageSieve (RFC 5804) has no "append a rule"
operation: `PUTSCRIPT` replaces a script wholesale. And `sievelib`'s
`factory.FiltersSet` makes *generating* a fresh script trivially easy.

So the cheapest implementation — build the rule set we want, `PUTSCRIPT` it —
is also the one that **silently deletes every filter the user made by hand**.
The failure mode is the bad kind: `PUTSCRIPT` succeeds, the tool reports
success, and the loss only surfaces days later when mail stops being sorted
and nobody connects it to a command that "worked".

## Decision

**Always parse the existing active script, merge the rule into the parsed set,
and render the whole thing back.** Back up the previous script before every
upload.

**What "survives" means, precisely** — the original wording here was *"rules
the tool did not write survive verbatim"*, and that claim was too strong. What
the code actually guarantees, and what it does not:

| Survives a merge | Does **not** survive |
|---|---|
| Rule **bodies** — every test, comparator, and action, unchanged | Free-standing comments, e.g. `# this one is for the accountant` |
| Rule **names**, in either dialect (see below) | Original whitespace and formatting, which are normalized |

The gap is `sievelib`'s renderer: `tosieve` re-emits only the name and
description markers, so any other comment in the user's script is dropped on
the first merge. That is a real loss of the user's *intent* even though no
rule is lost, and it surfaces the same way this ADR describes — quietly, days
later. It is tracked as a defect in [#7](https://github.com/harleypig/mxroute-email-filters/issues/7) rather than left
implied by an over-strong claim here.

**Rule identity spans two dialects.** Roundcube's managesieve plugin — the
webmail MXroute ships — names rules `# rule:[NAME]`, while `sievelib` writes
`# Filter: NAME`. mxfilter **reads both** and **writes Roundcube's**, so the
panel and this tool are co-editors of one file rather than each mangling the
other's work. Before that translation existed, a Roundcube-authored name was
silently replaced with `Unnamed rule N`, which broke the *Rules need stable
identity* consequence below in the worst way available: `--replace` could not
see the collision, appended a second rule that never fired because the
original carried `stop`, and reported success.

**A parse failure is a hard stop.** It is never a fall-back to overwrite. This
clause is the load-bearing half of the decision: the tempting recovery from
"`sievelib` cannot parse this script" is "then just write ours", which
converts a visible error into exactly the data loss this ADR exists to
prevent — and does so at the moment it is *most* likely, because an unusual,
hand-written script is both the hardest to parse and the most valuable to
keep. Fail, name the script and the reason, and let the user decide.

## Alternatives rejected

- **Overwrite with a generated script.** Simplest, and what the `sievelib`
  factory invites. Rejected for the silent destruction described above.
- **Write to our own script and activate it.** ManageSieve supports several
  stored scripts with one active, so keeping our rules in a separate script
  looks like clean isolation. It is not: only **one** script is active, so
  activating ours **deactivates** theirs — the same loss with an extra step —
  and it fights the panel, which would go on editing a script that no longer
  runs while showing filters that do nothing. Sieve's `include` (RFC 6609)
  could in principle compose several scripts, but its availability on MXroute
  is **unconfirmed**, and a composition the panel does not understand breaks
  the next time the panel rewrites the active script.
- **Parse-fail, then overwrite as a fallback.** Rejected above; called out
  separately because it is the one that will be proposed again by someone
  fixing a parse bug under time pressure.

## Consequences

- **We depend on `sievelib`'s parser handling whatever Roundcube — or a
  human — wrote.** That is a real risk surface. The hard-stop clause does not
  remove it; it keeps it *visible* rather than silently destructive.
- **Every upload is preceded by a backup**, so a merge that produces something
  unwanted is recoverable from disk rather than from memory.
- **Rules need stable identity.** To update or remove its own rule without
  touching others, the tool needs to recognise it on the next run — hence
  named rules and the `# Filter:` marker comments `sievelib` writes.
- **The dry-run diff is the user-facing proof.** Showing old-vs-new script
  before uploading is what makes "it merged, it did not overwrite" checkable
  rather than asserted.
- **A future Sieve evaluator inherits this constraint.** Anything that reads
  the active script to reason about it (see [`../ICEBOX.md`](../ICEBOX.md))
  must handle scripts we did not write, for the same reason the merge must.

# Icebox

Deferred "not now / maybe-someday" work — revisited **only on the trigger
noted**, not on the general issue-triage cadence. Per the maintainer's
`code-style.md` (the `ICEBOX:` convention) and `todo.md` (deferred work is not
a TODO item). Each entry carries an `ICEBOX:` tag so a
`grep -rn "ICEBOX:"` scan for prior deferred decisions surfaces it.

## Terraform data-source shim

`ICEBOX:` terraform data source, external data source, provider resource,
terraform shim, expose filters to terraform — **trigger: a concrete need to
read filter state from Terraform.**

[ADR 0001](adr/0001-standalone-cli-over-provider-resource.md) rejected putting
filters in `terraform-provider-mxroute`, and rejected a
provisioner/external-program shim that would dress imperative work as
declarative state. The one sliver that survives that reasoning is a
**read-only** view — a data source that reports the current filter rules so
other configuration can reference them, with no apply-time side effects.
Nothing needs it today, and building it would re-introduce the transport
problem (a ManageSieve session inside an HTTP provider) for a convenience
nobody has asked for. Revisit only when something concrete would consume it.

## Sieve `vacation` (autoresponders)

`ICEBOX:` sieve vacation, autoresponder, out of office, auto-reply,
vacation extension — **trigger: MXroute is confirmed to enable the `vacation`
extension, *and* there is a real need for it.**

**Half this trigger has now fired.** A live `mxfilter test` on 2026-08-14
found `vacation` **advertised** by the server, so the availability question is
answered for that account. The second half — a real need — has not, and the
entry stays iceboxed on that alone.

The tool refuses `vacation` today and points at the control panel. That
refusal is a **conservative default, not a documented MXroute limitation** —
unlike `redirect`, no MXroute source says either way
([`.claude/CONVENTIONS.md`](.claude/CONVENTIONS.md) › *Confidence*). Both
halves of the trigger matter: an autoresponder that the server silently drops
is worse than no autoresponder, and the panel already does this adequately, so
confirmation alone is not a reason to build it.

## Sieve evaluation — "which filters would catch this message"

`ICEBOX:` sieve evaluation, sieve interpreter, evaluate sieve, which filter
matches, match existing filters, sifter, sifter3, python-sifter — **trigger:
work actually starts on the front-end below, which depends on it.**

Answering *"which of my existing filters would catch this message?"* means
**evaluating** arbitrary Sieve rules against a message. The current design
deliberately avoids that: the tool holds the criteria in hand and translates
them to IMAP `SEARCH` keys, so no Sieve interpreter is ever in the loop — it
only ever has to *generate* Sieve, never *run* it.

The only Python Sieve evaluator is `python-sifter` / `sifter3`, which is
roughly three years stale and has documented gaps in the **RFC 5228 base
spec** — encoded characters, multi-line strings, bracketed comments, and the
`envelope` test. Adopting it therefore likely means patching or vendoring it,
which is a materially different commitment from adding a dependency.

**Surveyed 2026-08-13, and that framing is now too pessimistic — writing our
own evaluator is off the table.** Two maintained engines evaluate a real
message and are drivable from Python, so the choice is which to drive, not
whether one exists:

* **Dovecot Pigeonhole `sieve-test`** (LGPL-2.1, actively maintained) —
  *simulation is its default mode*; `-e` is what makes it execute. It needs
  no mail-store access and no admin rights, and `-Tlevel=matching` prints the
  tests performed **and the values they matched**, which is precisely the
  question this entry asks. It is also the engine MXRoute itself runs, so its
  answer is the server's answer rather than an approximation. Cost: it
  requires Dovecot installed (it `execv`s a hardcoded `/usr/bin/doveconf`),
  and it writes a compiled `.svbin` beside the script — run it against a copy
  in a temp directory.
* **`migadu/go-sieve`** (MIT, maintained by a commercial mail host) — vendors
  Pigeonhole's own `.svtest` conformance suite and passes **307 subtests**. It
  closes every `sifter3` gap listed above, exposes an ordered
  `AppliedActions`, and its `cmd/sieve-run` builds to a ~3.6 MB static binary
  with no runtime dependencies. Being MIT, it can be vendored outright.

`stalwartlabs/sieve` has the widest RFC coverage of anything surveyed and is
**rejected on licence**: AGPL-3.0-only against this tool's MIT. It also ships
no CLI.

**Decided 2026-08-14 — see [ADR 0004][adr4].** `migadu/go-sieve` is adopted
as the engine, behind a JSON-emitting wrapper we own, with Dovecot's
`sieve-test` kept as an optional differential oracle rather than the primary.
Verified before deciding: MIT, 307 Pigeonhole conformance subtests passing,
4.9 MB static binary, and correct on this account's real script including
implicit keep.

What remains iceboxed is the **feature**, not the engine choice: nothing is
built until the work is actually requested.

[adr4]: adr/0004-adopt-go-sieve-as-the-evaluation-engine.md

This is now an **anticipated** requirement rather than a speculative one,
because the front-end direction below depends on it. It stays iceboxed all the
same — nothing is built until that work is actually requested. Whatever
eventually reads the active script inherits
[ADR 0002](adr/0002-non-destructive-script-merge.md)'s constraint: it must
cope with scripts we did not write.

## TUI front-end

`ICEBOX:` TUI, terminal UI, interactive UI, front-end, curses, textual —
**trigger: the operator actually requests it.**

A stated longer-term direction: a terminal client that skims a message,
creates and manages filters matching it, shows which existing filters would
catch a given message (the entry above), and bulk-moves matching mail. It is
**not** intended to replace MXroute's web UI, at least initially.

**Deferred, and deliberately not designed for.** No framework has been chosen
and none should be until the feature is requested. One architectural
constraint is being honoured **now**, purely because it is cheap now and
expensive later: the core modules return structured data and never print — all
rendering, prompting, and progress output lives in `cli.py`
([`.claude/CONVENTIONS.md`](.claude/CONVENTIONS.md) › *The core returns data;
only the CLI prints*). That convention stands on its own merits (the core is
testable without capturing stdout), so it costs nothing if the front-end is
never built — and if it is, the core does not have to be torn apart first.

## Declarative rules in YAML — the `gmailctl` model

`ICEBOX:` declarative rules, yaml rules, rules as code, desired state, replace
all filters, import existing filters, round-trip sieve to yaml, gmailctl,
sieveruler — **trigger: requested directly. Rated more likely than the TUI
above.**

Define the whole filter set in a human-readable file (probably YAML), generate
the Sieve script from it, and **replace** the server's rules with the
generated set — plus an `import` that reads the account's current rules back
*into* that file so adoption is not a retyping exercise.
[`gmailctl`][gmailctl] is the UX to copy: a declarative config, a `diff`
against what is deployed, then an `apply`.

### Which format — surveyed 2026-08-14, deliberately NOT decided

Nothing in the tool leans either way (`criteria.Criteria` plus an action list
is the representation either format deserializes into), so this was left open
on purpose rather than decided early. The survey, so it need not be redone:

**gmailctl writes its config in Jsonnet, not YAML, and deliberately** — filter
sets are repetitive, and a plain data format gives you no way to factor out a
shared condition or name a list of correspondents once.

**But the round-trip should decide this, not the ergonomics.** The safety
invariant above is *replace only what you imported*, and import has to
**write** this file. **Data round-trips; programs do not.** If the format is
Jsonnet, importing means generating a *program*, so any abstraction written by
hand flattens back to literals on the next import — refactor, import, and the
factoring evaporates. Same class of quiet loss as the Roundcube rule names,
except structural rather than fixable.

| | YAML | Jsonnet |
|---|---|---|
| Learning curve | none | a real language |
| Abstraction | none (anchors give a little, no parameters) | functions, variables, imports |
| Round-trips on import | **yes** | **no** — hand-written abstractions flatten |
| Comments survive import | yes, via `ruamel.yaml` | n/a |
| Dependency | pure Python | a C++/Go binding |

One YAML footgun is specific to this domain and will bite exactly once:
**`*` is YAML's alias sigil, and Sieve `:matches` patterns start with `*`.**
`from: *@lists.example.com` is a parse error; `from: "*@lists.example.com"`
is fine. A schema plus a documented quoting rule handles it.

#### The two-layer escape hatch — how to get both

The trap is assuming the format mxfilter *reads* must also be where shortcuts
are written. It does not have to be, and separating the two dissolves the
question:

- **Layer 1 — what mxfilter reads: a plain list of rules.** Dumb data. No
  variables, no functions, nothing clever. The only thing mxfilter ever has to
  understand.
- **Layer 2 — optional, and nothing to do with mxfilter: whatever produced
  that list.** If writing twelve near-identical rules by hand gets annoying,
  write something that prints layer 1 and pipe it in:

  ```bash
  jsonnet filters.jsonnet | mxfilter apply -f -
  ```

  Jsonnet, Python, a template, `make` — mxfilter cannot tell the difference,
  because all it ever sees is layer 1.

So abstraction is available whenever it is wanted, without the tool growing a
language, and **import always has a faithful target to write**: it regenerates
layer 1, which is data and round-trips cleanly.

**The honest cost:** import gives back layer 1 only. It cannot reconstruct a
generator, so re-importing after editing filters in webmail means reconciling
that against layer 2 by hand. Visible and understood — which is the whole
difference from the silent flattening that Jsonnet-as-input causes.

**The one thing this asks of the implementation:** accept the rule list on
**stdin** as well as from a file, so layer 2 is possible from day one. Nothing
else about the decision needs settling up front.

### The acceptance case is already reserved

The live account has three rules that differ only by key and share an action —
`to :contains <address>` → `fileinto "Trash"; stop`, for Herrschners, Rumble,
and an Arch list. They are **deliberately left unmerged**, held as the
real-data acceptance case for this work (see [#21][i21], which would otherwise
propose merging them tomorrow).

They earn that role because **they force the round-trip test to be about
behaviour rather than bytes.** The naive test — assert `import → emit`
reproduces the input — passes trivially on a rule set nothing transforms, and
would *fail* here, since three rules correctly come back as one with a
three-key list. So the assertion has to be *"the emitted script sorts the same
mail to the same places"*, which is both harder and the only version that
actually tests the safety claim.

That matters because it is the same claim the **replace only what you
imported** invariant rests on. Merging makes the output deliberately not
byte-identical to the input, so byte-equality cannot be the safety argument —
behavioural equivalence has to be, and this case demands it up front rather
than leaving it to be discovered when someone's byte comparison goes red.

It is format-agnostic: it tests the round trip whichever way the YAML/Jsonnet
question above lands.

[i21]: https://github.com/harleypig/mxroute-email-filters/issues/21

Prior art for the Sieve half exists
too — [Transiever.SieveRuler][sieveruler] builds Sieve from provider-neutral
JSON rule documents and *reconciles* them against the deployed script rather
than blindly overwriting.

**This inverts [ADR 0002](adr/0002-non-destructive-script-merge.md), and the
inversion is the whole design problem.** That ADR mandates a non-destructive
merge precisely because rules the tool did not write must survive. Declarative
mode deliberately destroys them — that is what "desired state" means. The two
are reconcilable, but only through one invariant:

> **You may replace only what you successfully imported.**

Import fidelity is therefore not a convenience feature, it is the safety
precondition for replacement. A lossy import plus a faithful replace equals
silent deletion of every rule the importer could not represent — and the rules
most likely to be unrepresentable are the hand-written exotic ones, which are
exactly the ones worth keeping. Concretely, this needs:

* a **round-trip test** in both directions — `yaml → sieve → yaml` stable, and
  `sieve → yaml → sieve` semantically identical;
* an explicit **refusal to apply** when import could not fully represent the
  current script, naming what it could not read, rather than proceeding;
* a **diff against deployed state** shown before every apply, as `gmailctl`
  does.

**Drift is the other half.** Once both modes exist, a rule added imperatively
with `mxfilter add` is absent from the YAML, so the next `apply` deletes it —
the ordinary desired-state drift problem. Either the imperative commands learn
to write back to the YAML, or declarative mode owns the account exclusively
and says so. Decide that before building, not after.

The current design is already amenable: `criteria.Criteria` plus an action
list is the intermediate representation a YAML document would deserialize
into, so nothing needs restructuring today.

**The YAML file does not belong in this repository.** It holds the operator's
real filters and correspondents. It lives beside the account config outside
the repo, for the same reason
([`.claude/CONVENTIONS.md`](.claude/CONVENTIONS.md) › *Credentials*).

[gmailctl]: https://github.com/mbrt/gmailctl
[sieveruler]: https://github.com/SeWieland/Transiever.SieveRuler

## Per-folder retention — expire mail after N days

`ICEBOX:` retention, expiry, expire, auto-delete, age out, prune folder,
cleanup old mail, time limit, TTL, mailbox housekeeping — **trigger: requested
directly.**

Give a folder a maximum age and let mxfilter enforce it: `Github.Notifications`
holds nothing older than seven days, `Github.Billing` keeps everything.

**It is not filtering, and the difference is the point.** A Sieve rule decides
where a message goes **at delivery**, once. Retention acts on mail that is
already filed, **repeatedly, as it ages** — the same message is untouched on
day six and deleted on day eight. Sieve cannot express it: there is no verb
that runs later.

Server-side expiry does exist — Dovecot's `expire` plugin — but it is
configured by the **server administrator**, so it is unavailable on shared
hosting. Client-side is the only path, which is the same reason the
retroactive pass exists at all.

### The machinery is already here

`SEARCH BEFORE <date>` in a folder, then act on what comes back. That is the
retroactive pass with an age predicate instead of a criteria set, so this
mostly reuses what exists rather than adding a subsystem.

### It would be the most dangerous thing the tool does

Everything else is reversible. A move can be moved back; a bad Sieve rule can
be replaced from a backup. **This deletes mail on a schedule,
unattended**, and a wrong number in a config file is discovered only by
noticing an absence,
which nobody does. Design constraints follow from that:

- **Retention is opt-in per folder, and the default is keep forever.** Never a
  global default, never inherited by a subfolder — `Github.Billing` must not
  acquire a policy because `Github` has one.
- **Move to Trash, do not expunge.** Trash has its own expiry, so this becomes
  two independent decisions instead of one irreversible act, and leaves a
  window to notice a mistake.
- **Dry-run is the default for a first run against a folder**, and the preview
  says how many and how old — "delete 340 messages older than 7 days from
  Github.Notifications" is checkable in a way "apply retention" is not.
- **Never act on a folder not named in the config**, even for a rule that
  would match. Retention is explicit or it does not happen.

### Open

- **Where does the schedule live?** Retention only means anything if it runs
  repeatedly, and mxfilter is a one-shot CLI. Cron or a systemd timer keeps
  the tool a CLI; a daemon is a different product. Almost certainly the
  former, but
  it decides how the config and reporting are shaped.
- **Age from what?** `INTERNALDATE` (when the server received it) or the `Date:`
  header (what the sender claimed)? They differ, and the header is
  attacker-controlled. `INTERNALDATE` is almost certainly right, and is what
  `BEFORE` uses.
- Whether "keep the newest N" is wanted as well as "keep the last N days".

Prior art is client-side, not server-side: Thunderbird and Claws both offer
per-folder message expiry, which is evidence the shape is right and worth
reading before designing the config.

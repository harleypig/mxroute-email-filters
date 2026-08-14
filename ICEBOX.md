# Icebox

Deferred "not now / maybe-someday" work — revisited **only on the trigger
noted**, not on the general [`TODO.md`](TODO.md) cadence. Per the maintainer's
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

The shape this suggests, when the trigger fires: prefer `sieve-test` when
Dovecot is present because it is bit-for-bit what the server does, and fall
back to a bundled `go-sieve` binary so the tool works with nothing installed.

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
against what is deployed, then an `apply`. Note what it chose to *write* that
config in, though — **Jsonnet, not plain YAML**, and deliberately: filter sets
are repetitive, and a plain data format gives you no way to factor out a
shared condition or name a list of correspondents once. Either accept the
repetition, or pick a format with variables and functions. Worth settling
before the schema is designed, because it is expensive to change afterwards.
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

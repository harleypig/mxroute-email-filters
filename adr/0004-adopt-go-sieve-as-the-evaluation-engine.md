# 4. Adopt `migadu/go-sieve` as the Sieve evaluation engine

- Status: accepted
- Date: 2026-08-14

## Context

`mxfilter` currently runs **two matching engines** from one criteria model.
`criteria.sieve_conditions()` generates Sieve for the server to run, and
`criteria.imap_search_key()` plus a Python post-filter re-implements Sieve
matching semantics client-side so the retroactive pass can select the same
mail.

That second engine is a standing correctness risk, and the earlier framing of
it here — that one source of truth "keeps the two halves in agreement" — was
too comfortable. One *source* does not make two *implementations* agree.
A handful of tests pin specific cases; nothing enforces the general property.
The retroactive pass can only ever **approximate** what Dovecot does, and the
gap is in matching semantics, where being subtly wrong is indistinguishable
from being right until mail goes to the wrong place.

Evaluating the **actual script** against fetched messages deletes the second
engine outright. It also answers *"which of my filters would catch this
message?"* as a side effect, which is the same capability from the other
direction.

## Decision

**Adopt [`migadu/go-sieve`](https://github.com/migadu/go-sieve) as the Sieve
evaluation engine, behind a thin JSON-emitting wrapper that we own.**

Verified rather than researched, 2026-08-14:

- **MIT.** Fork of `foxcpp/go-sieve`, maintained by Migadu, a commercial mail
  host — so it has a production consumer and a second lineage upstream.
- **307 Pigeonhole conformance subtests pass.** Confirmed by running them: the
  repo vendors `dovecot/pigeonhole` as a git submodule and executes its own
  `.svtest` corpus. This is Dovecot's suite, not a self-authored one.
- **4.9 MB, statically linked, zero runtime dependencies** (`CGO_ENABLED=0`).
- **Correct on this account's real script**, first run — a GitHub notification
  filed to `Github.Notifications`, a `herrschners@` message to `Trash`, and an
  unmatched message yielding **implicit keep**. It parsed the Roundcube
  `# rule:[...]` dialect without complaint.
- ~6 µs per message.

**We write the wrapper; we do not parse the bundled CLI.** `cmd/sieve-run`
prints for humans (`fmt.Println("fileinfo:", data.Mailboxes)` — mislabelled,
`fileinto` is the Sieve action) and is plainly a demo rather than an
interface. Parsing that couples us to unstable, unversioned text. The library
API underneath is small and typed — `Load()`, `NewRuntimeData()`, and an
`interp.RuntimeData` holding the results — so a wrapper emitting JSON is a
short program over a stable surface.

This satisfies [ADR 0003][adr3]'s four conditions: a concrete better
implementation that does not exist in Python; a **process** boundary
exchanging JSON, not bindings; graceful degradation (no interpreter means no
evaluation features, everything else works); and distribution answered before
it lands ([#27][i27]).

## Alternatives rejected

- **`python-sifter/sifter3`** — the only Python evaluator. Roughly three years
  stale, with documented gaps in the **RFC 5228 base spec**: encoded
  characters, multi-line strings, bracketed comments, the `envelope` test.
  Adopting it means patching or vendoring it, which is a larger commitment
  than shelling out to a maintained binary and a worse one, because the gaps
  are exactly where silent wrongness lives. Staying pure-Python is not worth
  this.
- **Parsing `migadu/go-sieve`'s bundled CLI** instead of writing a wrapper.
  Rejected above: human-formatted, mislabelled, unversioned.
- **Dovecot's `sieve-test`** — the reference implementation, and *bit-for-bit
  what MXroute runs*, which is a real advantage no library can match. Rejected
  as the **primary** engine because it requires Dovecot installed (it `execv`s
  a hardcoded `/usr/bin/doveconf`) and writes a compiled `.svbin` beside the
  script. **Kept as an optional differential oracle**: where both are present,
  disagreement between them is a bug worth surfacing, and that is a cheap,
  high-value test we could not otherwise write.
- **`stalwartlabs/sieve`** — the widest RFC coverage surveyed, and **AGPL-3.0
  only** against this tool's MIT. Blocked on licence, not merit. It also ships
  no CLI.
- **Writing our own evaluator.** Rejected on the strength of the number above:
  307 conformance subtests is not a weekend's work to reproduce, and the
  failure mode of getting it subtly wrong is the one this ADR exists to
  remove.

## Consequences

- **The second matching engine goes away.** The retroactive pass stops
  approximating and starts asking what the script actually does. This is the
  point of the change; everything else is a cost or a bonus.
- **Two icebox entries are discharged by one dependency** — Sieve evaluation,
  and *"which filters would catch this message"*. The icebox had only ever
  evaluated these engines for the second.
- **It makes the backend seam tractable.** [#26][i26] requires a formalized
  `selects(rule, message)` operation, because the core cannot interpret
  provider-specific rule content. For the Sieve backend that is now
  authoritative rather than reimplemented.
- **Go becomes a build dependency of one component**, and the wrapper is ours
  to maintain. It is small, but it is not zero — and it is the first time this
  project owns code in a second language.
- **Distribution must be answered.** A statically-linked binary can be
  vendored or fetched, but "which platform, fetched how, verified how" is now
  a real question. [#27][i27] moves from nice-to-have to precondition.
- **Single-vendor risk**, mitigated but not removed: MIT means we can vendor
  it; the `foxcpp` upstream means there is a second lineage; a commercial mail
  host depending on it means it is unlikely to rot quietly.
- **The `.svtest` corpus becomes available to us.** Since the conformance
  suite is a submodule of a dependency we now track, a disagreement between
  our expectations and Pigeonhole's has a place to be caught.

[adr3]: 0003-python-core-with-per-component-language-choice.md
[i26]: https://github.com/harleypig/mxroute-email-filters/issues/26
[i27]: https://github.com/harleypig/mxroute-email-filters/issues/27

## Based on

- [ADR 0003](0003-python-core-with-per-component-language-choice.md) — this is
  the first component to invoke it, and the test of whether its four
  conditions are the right ones.
- [ADR 0002](0002-non-destructive-script-merge.md) — the evaluator reads
  scripts we did not write, so it inherits that constraint: it must cope with
  whatever Roundcube or a human put there. Verified against the live script on
  day one.

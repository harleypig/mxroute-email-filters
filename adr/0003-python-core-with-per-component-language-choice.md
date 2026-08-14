# 3. Python is the core; another language is allowed per component

- Status: accepted
- Date: 2026-08-14

## Context

The tool is Python, and that was a good choice: `sievelib` (MIT) and
`IMAPClient` (BSD-3) are the best available libraries for the two protocols
involved, both maintained, and `sievelib` already supports OAUTHBEARER/XOAUTH2
— which is what Gmail would eventually need. A survey across every language
found nothing better to switch to wholesale.

But two components are already visibly better served elsewhere, and neither is
hypothetical:

- **Sieve evaluation.** Python's only interpreter, `sifter3`, is roughly three
  years stale with documented gaps in the RFC 5228 base spec. Go has
  `migadu/go-sieve` — MIT, maintained by a commercial mail host, passing 307
  subtests of Dovecot Pigeonhole's own conformance suite, and building to a
  ~3.6 MB dependency-free static binary. That is not a marginal difference.
- **Heavy text work.** Sieve is a text format, and Perl's regex and text
  handling are genuinely more capable than Python's for the gnarlier cases.

So the question is not *"which language is the project"* but *"what happens
when a component is badly served by the project's language."* Answering it
once, in advance, is cheaper than answering it under pressure with a component
half-written.

## Decision

**Python is the core and the default.** Configuration, criteria, the
ManageSieve and IMAP sessions, and every front-end stay Python. A new
component is Python unless it clears the bar below.

**Another language is permitted for a component when all four hold:**

1. **A mature, permissively-licensed implementation exists there and does not
   in Python.** Not "would be nicer in X" — a concrete library or binary that
   is better by a margin worth paying for. The Sieve interpreter clears this;
   almost nothing else will.
2. **The boundary is a process, not a linkage.** The component runs as a
   subprocess exchanging structured data (JSON on stdin/stdout, or files).
   **No FFI, no bindings, no shared objects.** A process boundary keeps the
   component replaceable, independently testable, and free of ABI and build
   coupling; bindings make the foreign language a permanent build dependency
   of the whole tool.
3. **The core degrades without it.** If the component is absent, the tool says
   so and does less — it does not fail to start, and every path not needing
   that component keeps working. A missing Sieve interpreter should disable
   evaluation, not the tool.
4. **Distribution is answered before the component lands**, not after. See
   *Consequences*.

**The exchange format is data, not objects.** Whatever crosses the boundary
is serializable and versioned enough to notice a mismatch. A component that
needs to share a Python object with the core has failed test 2 and belongs
in Python.

## Alternatives rejected

- **Rewrite everything in Go.** Tempting once a Go component exists, and
  rejected on evidence: Go's ManageSieve and Sieve-generation libraries
  (`hstern/go-managesieve`, `hstern/go-sieve`) are genuinely good, genuinely
  six weeks old, zero-star, single-author, and self-described as
  pre-publication. `emersion/go-imap` v1 **silently truncates multi-response
  UID searches** — precisely this tool's workload. Trading `sievelib` 1.5.0
  for that is a downgrade wearing the clothes of a modernization.
- **Stay pure Python and accept `sifter3`.** Rejected: adopting a
  three-year-stale interpreter with known base-spec gaps means patching or
  vendoring it, which is a larger commitment than shelling out to a
  maintained binary — and a worse one, because the gaps are in matching
  semantics, where being subtly wrong is indistinguishable from being right
  until mail goes missing.
- **Bindings instead of a subprocess** (cgo/PyO3/ctypes). Rejected under
  test 2: it buys speed this tool does not need and costs a per-platform
  build matrix, an ABI surface, and the ability to run without the component.

**Not rejected here: a plugin architecture.** An earlier draft of this ADR
dismissed one as unrequested generality. That is no longer true — a
per-provider backend seam has been asked for, and it is a real question that
this ADR does not settle. The two are independent: this record governs *what
language a component may be written in*, and a backend seam governs *how the
core selects between implementations*. A backend seam would be Python either
way. See [#26](https://github.com/harleypig/mxroute-email-filters/issues/26).

## Consequences

- **Distribution gets harder, and that is the real cost.** A Python-only tool
  installs with `pip`. Add a Go binary and the answer becomes "which binary,
  for which platform, fetched how, verified how". A **Docker image** is the
  obvious mitigation, tracked in [#27](https://github.com/harleypig/mxroute-email-filters/issues/27) — it is the thing that makes
  polyglot practical rather than merely possible.
- **The static-binary property matters more than the language.** Go compiles
  to a dependency-free binary that can be vendored or fetched; Perl needs an
  interpreter, which is present on essentially every Linux host but is a
  runtime dependency all the same. Weigh that per language, not once.
- **Testing needs both toolchains**, and CI grows a second setup step. A
  component behind a process boundary can at least be faked cheaply in the
  Python tests, which is another argument for test 2.
- **A contributor may now need a toolchain they do not have.** Test 3 limits
  the damage: they can still run and test everything else.
- **The first candidate is the Sieve interpreter**, which is also the change
  that would collapse this tool's two matching engines into one. It is the
  worked example this ADR exists to authorize, and it should be the test of
  whether the four conditions are the right ones.

## Based on

- Survey of Sieve/mail tooling across languages, 2026-08-13/14 — the licence
  and maturity findings above, and the conclusion that the existing Python
  stack is the strongest available base.
- [ADR 0002](0002-non-destructive-script-merge.md) — anything that reads the
  active script inherits its constraint: it must cope with scripts we did not
  write. A foreign-language interpreter is no exception.

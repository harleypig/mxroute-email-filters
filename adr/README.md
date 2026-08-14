# Architecture Decision Records

Short records of significant, deliberate decisions for `mxfilter` — the
context, the choice, and why — so a considered "we decided X (and not Y)"
isn't re-litigated or lost. Format is lightweight [MADR][madr]. These are
**records of decisions already made**, not open work; open work lives in
the [issue tracker][issues], and deliberate "not now" deferrals in
[`../ICEBOX.md`](../ICEBOX.md).

| ADR | Decision |
|-----|----------|
| [0001](0001-standalone-cli-over-provider-resource.md) | Sieve filters live in a standalone CLI, not in `terraform-provider-mxroute` |
| [0002](0002-non-destructive-script-merge.md) | Merge into the active Sieve script; never overwrite it |
| [0003](0003-python-core-with-per-component-language-choice.md) | Python is the core; another language is allowed per component |
| [0004](0004-adopt-go-sieve-as-the-evaluation-engine.md) | Adopt `migadu/go-sieve` as the Sieve evaluation engine |

[madr]: https://adr.github.io/madr/

[issues]: https://github.com/harleypig/mxroute-email-filters/issues

# Architecture Decision Records

Short records of significant, deliberate decisions for `mxfilter` — the
context, the choice, and why — so a considered "we decided X (and not Y)"
isn't re-litigated or lost. Format is lightweight [MADR][madr]. These are
**records of decisions already made**, not open work; open work lives in
[`../TODO.md`](../TODO.md), and deliberate "not now" deferrals in
[`../ICEBOX.md`](../ICEBOX.md).

| ADR | Decision |
|-----|----------|
| [0001](0001-standalone-cli-over-provider-resource.md) | Sieve filters live in a standalone CLI, not in `terraform-provider-mxroute` |
| [0002](0002-non-destructive-script-merge.md) | Merge into the active Sieve script; never overwrite it |

[madr]: https://adr.github.io/madr/

# 1. Sieve filters live in a standalone CLI, not in terraform-provider-mxroute

- Status: accepted
- Date: 2026-08-13

## Context

This account's MXroute state is already managed as code by
[`terraform-provider-mxroute`][provider] — domains, mailboxes, forwarders,
catch-all, spam lists. Email **filters** were the obvious next thing to want
there, so the question was whether to add a filter resource to that provider
or to build a separate tool. Three findings decided it.

**1. The MXroute REST API exposes nothing for filters or Sieve.** Verified two
independent ways: the published OpenAPI document has 26 paths and none of them
is filter-related; and MXroute's own 4.0.1 changelog enumerates the API's
categories in full — Domains, Email Accounts, Forwarders, Spam Settings,
Catch-All, DNS Information, Quota, Reseller. There is no endpoint to call.

> **Wording trap.** That changelog describes *"Forwarders — set up and manage
> email forwarding **rules**"*. "Rules" there means forwarders, not Sieve
> rules. Do not read it as filter support and re-open this.

**2. ManageSieve fits Terraform's model poorly.** Filters are reachable only
over ManageSieve (RFC 5804) — a stateful, authenticated **session** protocol
(connect, STARTTLS, authenticate, `LISTSCRIPTS`, `GETSCRIPT`, `PUTSCRIPT`,
`SETACTIVE`). A provider is a stateless CRUD client reconciling discrete
resources against a desired state; here the unit of transfer is a whole script
blob, and that blob is **shared with Roundcube's filter UI**. So a resource's
desired state is not the file's desired state, and the reconcile that
Terraform would naturally perform is precisely the overwrite that
[ADR 0002][adr2] exists to forbid.

**3. The retroactive IMAP pass has no desired state at all.** Sorting mail
already delivered is inherently imperative, one-shot work. It converges on
nothing, and a `terraform apply` that moves a user's mail as a side effect of
reconciliation would be a genuinely unwelcome surprise. This half is the
reason the tool exists, and it is the half a provider cannot model.

## Decision

**Build a standalone Python CLI (`mxfilter`).** The provider keeps
account/domain/forwarder state as code; the CLI owns filter rules and
retroactive mail sorting. The boundary is not a preference — the API draws it
for us.

## Alternatives rejected

- **A resource in `terraform-provider-mxroute`.** Blocked outright by finding
  1: the provider is an HTTP client for `api.mxroute.com` and there is nothing
  to call. Making it work would mean bolting a second, unrelated transport — a
  ManageSieve session client on TCP 4190 — into an HTTP provider, and then
  still confronting findings 2 and 3.
- **A Terraform external data source / provisioner shim** wrapping this CLI so
  Terraform can invoke it. Rejected: it inverts the dependency (Terraform
  would need this binary installed and on `PATH`), it gives up plan-time
  visibility (an external program's effect is not planned), and it dresses an
  imperative action as declarative state — worse than admitting it is
  imperative. Not refused forever: a **read-only** data source over filter
  state is the plausible sliver, and it is iceboxed rather than dismissed
  (see [`../ICEBOX.md`](../ICEBOX.md)).
- **Do nothing — use Roundcube's filter UI by hand.** The honest status quo,
  and genuinely workable for the Sieve half; Roundcube writes the same script.
  It fails on the half that motivated the tool: the UI cannot apply a new
  filter **retroactively**, so every rule made there leaves existing mail
  untouched and the user sorting by hand. It is also neither reproducible nor
  reviewable, which is the reason this account is managed as code at all.

## Consequences

- **Two repos to keep coherent.** The boundary is written down in
  `.claude/CONVENTIONS.md` › *The sibling repository* so it stays a decision
  rather than an accident. In particular, **forwarding is pointed at, not
  reimplemented** — it is the substitute for the disabled Sieve `redirect`,
  and it is already the provider's `mxroute_forwarder` resource.
- **This tool carries credentials the provider never needs.** The provider
  authenticates with API-key headers; this one needs the **mailbox password**
  for both ManageSieve and IMAP. That is why credential discipline is this
  repo's hard boundary rather than a general good practice.
- **Filter state is not in Terraform state**, so it is not planned, not
  drift-detected, and not removed by `terraform destroy`. Accepted
  deliberately.
- **Revisit if MXroute ships filter endpoints.** That would remove finding 1,
  but not 2 or 3 — so it would make a provider resource *possible*, not
  obviously *right*. Re-argue it then; do not treat the endpoint's arrival as
  settling the question.

[adr2]: 0002-non-destructive-script-merge.md
[provider]: https://github.com/harleypig/terraform-provider-mxroute

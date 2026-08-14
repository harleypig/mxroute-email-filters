# mxfilter Conventions

Repo-specific conventions. The global `~/.claude/` config carries everything
generic (git/gh, code style, the Python toolchain via `python.md` +
`ruff.md`, the QA dimensions). This file records only what is specific to
**this** repo.

## What this is

`mxfilter` — an MIT-licensed Python CLI that manages MXroute email filters end
to end. It does two things, and the second is the reason it exists:

1. **Creates server-side Sieve filters over ManageSieve**, merging the new
   rule **non-destructively** into the account's existing active script (see
   *Rule conventions* below and [ADR 0002][adr2]).
2. **Applies the same rule retroactively over IMAP** to mail already sitting
   in the mailbox — because Sieve only ever runs on **new incoming** mail.
   Writing the Sieve rule alone leaves every message already delivered exactly
   where it was.

Distribution name and package are both `mxfilter`; the console entry point is
`mxfilter = "mxfilter.cli:main"`. It is **not published anywhere** — see
[RELEASING.md](../RELEASING.md).

Built on two libraries, both of which the code wraps rather than exposes:

- **`sievelib` (≥ 1.5.0)** — the ManageSieve client (`sievelib.managesieve`),
  the Sieve parser, and the `factory.FiltersSet` script builder.
- **`IMAPClient` (≥ 3.1.0)** — the IMAP half.

## Layout

- `mxfilter/__init__.py` — the package docstring, `__version__`, and
  `MxFilterError` (the one exception type every actionable failure raises).
- `mxfilter/config.py` — endpoint and credential resolution, and the `Secret`
  wrapper (see *Credentials* below).
- `mxfilter/criteria.py` — the shared criteria model, translated **both** to
  Sieve tests and to IMAP `SEARCH`. One model, two backends — this is what
  keeps the two halves in agreement.
- `mxfilter/sieve.py` — the ManageSieve session wrapper plus the offline
  script-editing helpers (parse / merge / render / diff / backup).
- `mxfilter/imap.py` — the IMAP session wrapper (folders, search, move, flag)
  and folder-name normalization.
- `mxfilter/cli.py` — argument parsing and the subcommand implementations.
- `mxfilter/__main__.py` — `python -m mxfilter`.
- `tests/` — pytest, mirroring the package layout ([TESTS.md](TESTS.md)).

The split is deliberate: the **offline** logic (criteria translation, Sieve
generation, folder-name normalization, script merging) is exercisable with no
server at all. Keep new logic on the offline side of that line wherever it can
live there.

## The core returns data; only the CLI prints

**`config`, `criteria`, `sieve`, and `imap` return structured values and raise
`MxFilterError`. Every piece of rendering, prompting, confirmation, and
progress output lives in `cli.py`.** Two reasons, both cashing out now: the
core stays testable without capturing stdout, and a future front-end can sit
on the same core instead of requiring it to be torn apart first.

The one exception is deliberate and stays narrow: `SieveSession._log` and
`ImapSession._log` emit `--verbose` protocol progress. It is confined to those
two helpers and gated on a flag — do not let a second output path grow beside
them, and do not add one to the offline helpers at all.

## The protocols

There is no vendor API here — the tool speaks two standard protocols.

- **ManageSieve** — RFC 5804. IANA reserved **TCP 4190** for it, and the RFC
  requires both ends to implement **STARTTLS** (*"Client and server
  implementations MUST implement the STARTTLS extension"*). That is the
  tool's default, and it is a **default, not a verified fact about MXroute**
  — see *Confidence* below.
- **IMAP** — port **993** (implicit TLS) or **143** (STARTTLS). The username
  is the **full email address**, and the hostname is **per-account**
  (MXroute's panel gives it as "the same as your primary MX record"), so it is
  always configuration and never a built-in default.

Every setting resolves highest-priority-first: a CLI flag → an `MXROUTE_*`
environment variable → the TOML config file
(`$XDG_CONFIG_HOME/mxfilter/config.toml`) → a built-in default. The password
is the exception and is handled separately (*Credentials*).

### Confidence — what MXroute actually documents

MXroute documents very little of its Sieve surface, and the gaps are
themselves a design driver (they are why *Discover, don't hardcode* below is a
rule rather than a preference). Mark these honestly; do **not** quietly
promote one tier to another.

**Confirmed:**

- **Sieve `redirect` is disabled.** MXroute's own blog says so — *"Why we
  disabled redirect sieve filters on MXroute"* (2024-03-21) — and gives the
  reason: real forwarders "are designed to properly handle SRS".
- **IMAP 993 / 143, full-email-address username, per-account hostname** (the
  panel's *Email Clients* page).
- **The REST API exposes nothing for filters or Sieve.** Verified twice, two
  ways: against the published OpenAPI document (26 paths, none filter-related)
  and against MXroute's own 4.0.1 changelog, which enumerates the API's
  categories in full — Domains, Email Accounts, Forwarders, Spam Settings,
  Catch-All, DNS Information, Quota, Reseller. This is the load-bearing fact
  behind [ADR 0001][adr1].
  - **Wording trap:** that changelog describes *"Forwarders — set up and
    manage email forwarding **rules**"*. "Rules" there means **forwarders**,
    not Sieve rules. Do not read it as filter support and re-open the
    question.

**Likely, not confirmed:**

- The folder hierarchy separator is **`.`** (Dovecot Maildir++), and the spam
  folder is **`INBOX.spam` — lowercase, not `INBOX.Junk`**. The evidence is an
  incidental filesystem path in a blog post (`.../Maildir/.INBOX.spam`), not a
  documented statement. Treat it as a hint that makes a runtime-discovered
  value plausible, never as something to hardcode.

**Unconfirmed — say so plainly rather than filling the gap:**

- **The ManageSieve port and its TLS mode.** 4190 + STARTTLS is the
  IANA/RFC 5804 and Dovecot default; MXroute documents **neither**. Both are
  configurable (`--sieve-port`, `--sieve-tls`) precisely because of this.
- **Whether `notify` is disabled.** No MXroute source says either way — same
  for `vacation`, `extlists`, and `spamtest`. The tool refuses `notify` and
  `vacation` as a **conservative default** with a message pointing at the
  control panel; that is our choice, not a documented MXroute limitation.
- **ManageSieve script-size, script-count, and rate limits.**
- **Which script name Roundcube's managesieve plugin writes.** It is a
  server-side configuration value (`managesieve_script_name`), so it is
  discovered, not assumed.

### Discover, don't hardcode

**Server capabilities, the active script name, and the folder delimiter are
discovered from the server at runtime — never hardcoded.** This is a named
convention, not a style preference, and the reason is concrete rather than
abstract: MXroute has publicly stated it intends to migrate away from
DirectAdmin, Crossbox, and **Roundcube** this year, and is mid-migration from
**Dovecot 2.3 to 2.4**. Runtime discovery survives both migrations; a baked-in
constant does not.

In practice:

- Take the active script from the server's own listing, not a constant.
- Take the folder delimiter from the server's folder list, and normalize
  user-supplied names against it — so `Lists/GitHub` and `INBOX.Lists.GitHub`
  name the same folder (`imap.normalize_folder`).
- Read the advertised Sieve extensions rather than assuming a capability is
  present.

A default is fine where the protocol supplies one (port 4190); an **assumption
about MXroute's configuration** is not.

## Rule conventions

- **Merge, never overwrite.** A new rule is merged into the parsed existing
  script; rules the tool did not write survive untouched. Roundcube's filter
  UI writes the same script, so overwriting silently destroys a user's
  hand-made filters. A parse failure is a **hard stop**, never a
  fall-back-to-overwrite. See [ADR 0002][adr2].
- **Back up before every upload.** The previous script is written to the
  backup directory before the new one is sent.
- **Show, then change.** Every mutating subcommand works out what would
  change, shows it (a diff for the script, a preview for the messages), and
  only then applies it. `--dry-run` stops after the "show it" step.
- **Refuse the actions we will not emit, with a pointer** — and keep the two
  reasons for refusing apart, because the distinction is exactly the
  confidence tiering above:
  - `sieve.MXROUTE_FORBIDDEN_ACTIONS` holds **`redirect` alone**. It is the
    only action refused because MXroute is *confirmed* to disable it, and its
    message points at forwarders (the panel, or the `mxroute_forwarder`
    Terraform resource).
  - `sieve.UNIMPLEMENTED_ACTIONS` holds **`notify` and `vacation`**. These are
    refused because *we* do not generate them, and their message says so
    explicitly rather than implying an MXroute restriction — no source
    confirms or refutes their availability.

  Collapsing the two would restate an unverified assumption as a server fact,
  which is the failure this repo's *Confidence* discipline exists to prevent.
- **Supported and used:** `fileinto`, `discard`, `stop`, `keep`, and flag
  actions.
- **One criteria model, two translations.** A new matching capability is added
  to `criteria.py` and translated to *both* Sieve and IMAP `SEARCH` — never to
  one side only. Where IMAP `SEARCH` is coarser than the Sieve comparator, the
  results are re-checked client-side against the real Sieve semantics so the
  retroactive pass matches what the filter will do going forward.

## Credentials

**The mailbox password never reaches stdout, stderr, a log, or a transcript.**
This is the hard boundary of this repo, the analogue of a write-only secret,
and it is enforced by construction rather than by care:

- `config.Secret` wraps the password and overrides **both** `__str__` and
  `__repr__` to `<redacted>`, which covers every accidental disclosure path —
  `print`, an f-string, `%s` in a log line, and a `repr` in a traceback frame.
- The real value is reachable **only** through `Secret.reveal()`, which is
  greppable and therefore reviewable. Call it only when handing the password
  to a connection method — never to display, log, or format it.
- The password is sourced from `MXROUTE_PASSWORD`, `MXROUTE_PASSWORD_CMD` /
  `--password-cmd` (preferred — it keeps the value out of the environment), or
  an interactive `getpass` prompt. It is **never** read from the TOML config
  file.
- The same bar binds test doubles and throwaway debug shims. To tell two
  credentials apart, emit a non-reversible discriminator (a literal
  `set`/`unset`, a length, a short hash prefix) — never the value. See the
  global `CLAUDE.md` *Secret Handling*.

## The sibling repository

[`terraform-provider-mxroute`][provider] and this tool are **complementary,
not overlapping**, and the boundary is clean because the API draws it for us:

| Repo | Owns |
|------|------|
| `terraform-provider-mxroute` | account/domain state as code — domains, mailboxes, **forwarders**, catch-all, spam lists, pointers |
| `mxfilter` (here) | filter **rules** (Sieve) and retroactive mail sorting (IMAP) |

Two practical consequences:

- **Forwarding belongs there, not here.** It is the substitute for the
  disabled Sieve `redirect`, and it is already implemented as the
  `mxroute_forwarder` resource. Point users at it; do not reimplement
  forwarding in this tool.
- **Filters cannot belong there.** The REST API the provider is built on has
  no Sieve surface at all ([ADR 0001][adr1]).

## Toolchain & reproducibility

- **Python ≥ 3.11** (`requires-python` in `pyproject.toml`), developed inside
  a **`.venv`** at the repo root — never against the system Python.
- **`uv` provisions it** (the isolated-app rung of the global
  `toolchain-provisioning.md` ladder):

  ```sh
  uv venv                       # create .venv
  uv pip install -e '.[dev]'    # the package plus the dev tools
  ```

- **Runtime dependencies are `sievelib` and `IMAPClient`, and that is
  deliberate.** Both are pinned by lower bound in `pyproject.toml`. Adding a
  third runtime dependency to a tool whose whole job is two protocol
  conversations deserves an argument first.
- Dev tooling (`ruff`, `pytest`) is an **optional dependency group**, so a
  user installing the CLI never pulls the linter in.

## QA

The global `qa.md` owns the pipeline and its ordering; this section is the
concrete toolchain and the **status of every dimension** for this repo.

- **Format + lint:** `ruff` — `ruff format` and `ruff check`, configured under
  `[tool.ruff]` in `pyproject.toml`. Pre-commit gates both, in the two-file
  fix-then-check split (`.pre-commit-config-fix.yaml` runs the auto-fixers
  once as a prep step; `.pre-commit-config.yaml` is the non-modifying gate).
  Do **not** wire black/isort/flake8 alongside ruff (`python.md`).
- **Code smell / complexity:** ruff's `B` (bugbear), `C4`, `SIM`, `UP`, and
  `RUF` rule sets, inside the same `ruff check`.
- **Security:** `gitleaks` + `detect-private-key` in pre-commit (secrets).
  Note that the *most* important security property of this repo — the password
  never being emitted — is a code-structure guarantee (`Secret`), not
  something a scanner checks; review it by reading `Secret.reveal()` call
  sites.
- **Prose:** `markdownlint` and `yamllint` in pre-commit.

Full dimension status:

| Dimension | Status |
|-----------|--------|
| 1. Format | **Active** — `ruff format` |
| 2. Lint | **Active** — `ruff check` |
| 3. Type-check | **Planned** — the package is fully annotated but nothing gates it; wire `pyright` (`pyright.md`) alongside the test suite ([TODO][todo]) |
| 4. Code smell / complexity | **Active** — ruff `B`/`C4`/`SIM`/`UP`/`RUF` |
| 5. Security | **Active (secrets only)** — `gitleaks`, `detect-private-key`. SAST is **Off**: the attack surface is two outbound TLS client sessions and no untrusted input parsing beyond the user's own Sieve script |
| 6. Tests | **Active** — the offline tier is green (`make test` / `pytest`); see [TESTS.md](TESTS.md) |
| 7. UI/UX & accessibility | **N/A** — a CLI with no UI. Terminal output legibility is covered by the *show, then change* convention |
| 8. End-to-end | **Scaffolded, gated** — `tests/live/` exists and skips unless `MXFILTER_LIVE=1`; it has never run against a real account ([TODO][todo]) |
| 9. Compatibility | **N/A** — single target (CPython ≥ 3.11); no external contract we publish |
| 10. Performance & load | **N/A** — interactive, single-mailbox, human-scale. Revisit only if a retroactive pass over a very large folder proves slow |
| 11. Reliability & observability | **N/A** — a one-shot CLI, not a service. Its reliability property is the backup-before-upload convention |
| 12. Build | **N/A** — pure Python, no build step (`setuptools` metadata only) |
| 13. Documentation | **Active** — this file, `README.md`, and `adr/`; markdownlint gates the prose |
| 14. Code review | **Informal** — solo repo; `master` is PR-only, 0 required reviewers |
| 15. CI | **Active** — `.github/workflows/test.yml` runs `ruff check`, `ruff format --check`, and `pytest` on every PR and on pushes to `master`. The live tier is deliberately excluded — it needs real credentials |

## Merge policy & versioning

- **`master` is PR-only**, enforced today by the local `no-commit-to-branch`
  pre-commit hook and the global `branch-protection.py` edit-time hook. There
  is **no server-side ruleset yet** — that is the missing layer, and until it
  exists the guard is local-only and anyone without the hooks installed can
  still push (`git.md` *Protecting the Default Branch*).
- **Neither merge sentinel is declared, and that is a precondition failure,
  not an oversight.** The auto-merge opt-in (`gh.md`) rests on server-side
  guardrails making a manual merge gate redundant; there are none here yet, so
  the line does not go in. The merge-finalization enforcement hook likewise
  waits on there being a merge pipeline to backstop. Both become live
  questions the moment a ruleset and CI exist — not before.
- **Versioning:** semver `vX.Y.Z`, `repo` scope (one version for the whole
  tool — the `git.md` *Versioning & tags* method). See below.

## Versioning & tagging

`repo`-scope semver, tagged `vX.Y.Z`, annotated, cut at the merge commit on
`master` with the `release-tag` skill.

- **Currently `v0.y.z` — alpha.** Per `git.md`, `X = 0` means **breakage is
  expected** and the `y.z` split is deliberately loose: bump `y` for a
  meaningful addition, `z` for a smaller change, and do not agonize over
  which. The `0 → 1` jump is a decision in its own right and is not near.
- **No API-major alignment.** The sibling provider aligns its MAJOR to the
  MXroute REST API's major, because it is a client of a versioned API. That
  does **not** carry over: this tool speaks ManageSieve (RFC 5804) and IMAP,
  standardized protocols with no vendor version to track. Do not import the
  provider's bump policy.
- **A tag publishes nothing.** There is no release pipeline and no registry —
  a tag is a marker on history, so it is cheap and carries no
  cannot-be-unpublished risk. See [RELEASING.md](../RELEASING.md).

[adr1]: ../adr/0001-standalone-cli-over-provider-resource.md
[adr2]: ../adr/0002-non-destructive-script-merge.md
[provider]: https://github.com/harleypig/terraform-provider-mxroute
[todo]: ../TODO.md

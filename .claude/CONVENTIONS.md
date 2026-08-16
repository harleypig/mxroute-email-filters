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

### The scoping rule

Stated by the operator, 2026-08-14, and recorded because it settles scope
questions rather than merely describing the tool:

> The general purpose of this app is to create ways of managing filters and
> settings, either by switch or automation.

Read it as a **test to apply**, not a mission statement. "Is this in scope?"
becomes *is it a filter or a setting, and is there no way to manage it here?*
— and if so, the answer is yes, without needing a decision.

Two words carry the weight:

- **Settings, not just filters.** A folder's subscription state, a rule's
  position, a capability the account has — anything the account holds that a
  user might want to change is in scope. The two halves in *What this is*
  above are what the tool was **built** for; they are not its boundary.
- **By switch or automation.** Both, and neither is the poor relation. A
  thing you can only do by hand and a thing you can only get as a side effect
  of something else are each half-built.

**What this rules out** matters as much: reading a setting, warning about it,
and telling the user to go fix it somewhere else. That is the shape a
management tool should not end on — it is a report wearing a command's
clothes.

It cuts the other way too. Scope is not *everything about email*: reading
mail, composing, an address book, and account provisioning are not filters or
settings on this account, and the sibling
[terraform-provider-mxroute][provider] owns account state as code (see *The
sibling repository* below).

**A worked example**, so this reads as a rule rather than a slogan. Four
observations came out of the subscription work
([#38](https://github.com/harleypig/mxroute-email-filters/issues/38)) and
were surfaced as open questions: no CLI way to subscribe an existing folder;
`folders` and `test` silent about subscription; an unsubscribed folder left
alone; a flag with no effect. Applying the rule, three are not questions at
all — subscription is a setting, so exposing it is in scope by definition
([#42](https://github.com/harleypig/mxroute-email-filters/issues/42)) — and
the fourth is a plain bug
([#43](https://github.com/harleypig/mxroute-email-filters/issues/43)).
Asking was the error.

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

### Confidence — documented, observed, and unknown

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

**Observed on one account — `mxfilter test`, 2026-08-14:**

A live read against a single MXroute server settled several of these. It is a
**separate tier on purpose**: an observation is stronger than a guess and
weaker than documentation, and it describes *that server* rather than MXroute.

| Observed | Value |
|---|---|
| ManageSieve port + TLS | **4190 + STARTTLS works** — the protocol default was right |
| Folder delimiter | **`.`** — Maildir++, as the blog-post path suggested |
| Spam folder | **`INBOX.spam` exists** |
| Active script name | **`managesieve`** |
| `vacation` | **advertised** |
| `enotify` | **not advertised** |
| `FILTER=SIEVE` | **not advertised** — no server-side retroactive filtering |
| `spamtest`, `extlists` | **not advertised** |
| `regex`, `mailbox`, `imap4flags`, `copy`, `envelope` | **advertised** |

**This does not license hardcoding any of it**, and the reason matters: the
tool discovers these at runtime not because we were unsure what *this* account
reports, but because MXroute is mid-migration on both its panel and Dovecot
(*Discover, don't hardcode*, below). An observation dated today says nothing
about the same server next quarter, and nothing about anyone else's server.
The script name in particular is a per-server configuration value
(`managesieve_script_name`), so it is the **least** generalizable item here.

**`vacation` being advertised changes the status of our refusal.** mxfilter
still refuses both `vacation` and `notify`, but they are no longer refusals of
the same kind: `enotify` is **not advertised** on this server, while
`vacation` **is** — so declining to emit it is a **deliberate choice of ours**
(the control panel does autoresponders, and a Sieve autoresponder has real
footguns), where declining an action the server never advertised is barely a
choice at all.

The shared refusal message is still correct for both and needs no tailoring:
it says the refusal is ours rather than a documented MXroute restriction, and
points at `mxfilter test` to find out what this server actually advertises.
Distinguishing the two in the message would bake a per-server observation into
a string, which is precisely what *Discover, don't hardcode* exists to
prevent.

Half the trigger on the `vacation` icebox entry has therefore fired; see
[ICEBOX.md](../ICEBOX.md).

**Unconfirmed — say so plainly rather than filling the gap:**

- **Whether any of the observations above generalize.** MXroute documents
  neither the ManageSieve port, the TLS mode, the delimiter, nor the extension
  set. Every one of those rows is one server on one day.
- **ManageSieve script-size, script-count, and rate limits.** Nothing in the
  `CAPABILITY` response speaks to these, so a live read cannot settle them.

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
  backup directory before the new one is sent, and `mxfilter backup` takes the
  same copy on demand. **One location, and it is the config directory** —
  `$XDG_CONFIG_HOME/mxfilter/backups`, beside `config.toml`
  (`config.default_backup_dir`), overridable by `--backup-dir` /
  `MXROUTE_BACKUP_DIR`. XDG would call a backup *state*; co-locating it with
  the config is a deliberate departure from XDG, not an XDG-endorsed reading,
  and the reason is that a backup the user cannot find is not a backup. Two
  defaults for one kind of file is how somebody ends up looking in the
  directory that does not have their backup in it.
- **A backup is the server's exact bytes.** `sieve.write_backup` writes what
  it was handed, with newline translation off, mode `0600` in a directory
  created `0700`. Nothing decorates it — `mxfilter show` adds banner lines for
  a reader and is therefore *not* a backup, which is exactly the trap
  redirecting `show` to a file used to set. There is no restore path: putting
  a file back needs another ManageSieve client, and building one is its own
  change ([#13][i13]).
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
- The password resolves through its own ladder, highest first: **an explicit
  flag** (`--password-file`, `--password-cmd`, `--password` — argparse makes
  them mutually exclusive) → `MXROUTE_PASSWORD_FILE` → `MXROUTE_PASSWORD_CMD`
  → `MXROUTE_PASSWORD` → `password_file` → `password_cmd` (config file) → an
  interactive `getpass` prompt. Two rules produce that order, and both are
  load-bearing:
  - **A flag beats an ambient variable.** It was typed for *this* run; the
    variable merely happens to be exported. The inverse — which is what the
    code did until the ladder was fixed — silently authenticates as the
    wrong account when `MXROUTE_PASSWORD` is exported for one mailbox and
    `--password-cmd` names another.
  - **A literal value never beats an instruction about where to fetch one.**
- The **literal password is never read from the TOML config file** — only
  `password_file` and `password_cmd` are. That is unchanged.
- **A password file is refused, not warned about, when its mode lets anyone
  else read it.** Any bit in `0o077` is a refusal naming the path, the mode,
  and the `chmod` that fixes it; the file is not opened at all. `libpq`
  applies the same rule to `~/.pgpass`, except that it ignores the file
  silently — here the file was named explicitly, so falling through the rest
  of the ladder without saying so would be worse than stopping. Note for
  WSL: a file on a Windows mount reports
  `0777` regardless of intent, so the file has to live on the Linux
  filesystem — do **not** add a filesystem exception to the check.
- **`--password` is deliberately the least safe rung and says so.** It exists
  because it was asked for; `cli.py` warns on stderr that an argument is
  visible in the process list and saved to shell history. The warning is
  presentation and stays in the CLI; the mode refusal is behaviour and stays
  in the core.
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

## Where work is tracked

Work is tracked as **GitHub issues**, not in a planning file. The sentinel
the global `todo.md` reads:

tracker: github

`TODO.md` is gone, and every open item it held was migrated to an issue. A
repo carrying both grows two answers to "what is left to do", and the file is
always the stale one — it has no assignee, no labels, no cross-references, and
nothing closes it when a PR lands.

Two things follow, which is why this is a declared sentinel rather than a
habit:

- **A captured follow-up becomes an issue**, reconciled against the existing
  ones rather than appended blindly.
- **Every issue carries a `role:*` label.** An open issue without one has not
  been triaged (`labels.md`), and that absence is the signal a sweep keys on.

**Deferred work is still not an issue.** A considered "not now" belongs in
[ICEBOX.md](../ICEBOX.md) with its trigger, or as an `ICEBOX:` marker at the
relevant code. Issues are for work someone intends to do; the icebox is for
decisions taken and parked. Filing a deferral as an issue is how a tracker
stops being readable.

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
| 3. Type-check | **Planned** — the package is fully annotated but nothing gates it; wire `pyright` (`pyright.md`) ([#10][i10]) |
| 4. Code smell / complexity | **Active** — ruff `B`/`C4`/`SIM`/`UP`/`RUF` |
| 5. Security | **Active (secrets only)** — `gitleaks`, `detect-private-key`. SAST is **Off**: the attack surface is two outbound TLS client sessions and no untrusted input parsing beyond the user's own Sieve script |
| 6. Tests | **Active** — the offline tier is green (`make test` / `pytest`); see [TESTS.md](TESTS.md) |
| 7. UI/UX & accessibility | **N/A** — a CLI with no UI. Terminal output legibility is covered by the *show, then change* convention |
| 8. End-to-end | **Scaffolded, gated** — `tests/live/` exists and skips unless `MXFILTER_LIVE=1`; it has never written to a real account ([#9][i9]) |
| 9. Compatibility | **N/A** — single target (CPython ≥ 3.11); no external contract we publish |
| 10. Performance & load | **N/A** — interactive, single-mailbox, human-scale. Revisit only if a retroactive pass over a very large folder proves slow |
| 11. Reliability & observability | **N/A** — a one-shot CLI, not a service. Its reliability property is the backup-before-upload convention |
| 12. Build | **N/A** — pure Python, no build step (`setuptools` metadata only) |
| 13. Documentation | **Active** — this file, `README.md`, and `adr/`; markdownlint gates the prose |
| 14. Code review | **Informal** — solo repo; `master` is PR-only, 0 required reviewers |
| 15. CI | **Active** — `.github/workflows/test.yml` runs `ruff check`, `ruff format --check`, and `pytest` on every PR and on pushes to `master`. The live tier is deliberately excluded — it needs real credentials |

## How work is dispatched

This repo is **team-managed**. The sentinel the global
`team-managed-delegation.md` reads:

team-managed: enabled

So the main-thread agent is the **Project-Manager seat**: it plans, routes,
and integrates, and **substantive edits are dispatched to the role that owns
them** — the Developer for package source and its tests, QA for the test
tier, the Writer for prose, the Config Engineer for anything under `.claude/`.
Orchestration glue stays with the main thread: planning notes, CI wiring,
`.claude/settings*.json`, and scratch files.

**Enabled deliberately, on 2026-08-14, not at scaffold time.** The rule is
default-off and says an agent must not self-enable it; the operator asked
for it after watching a module and its whole test suite get authored on the
main thread. Recording the date matters because the norm is what makes
*"since the role last touched these files"* a meaningful boundary, and before
this date there is no such boundary to compute.

The nudge is a `PreToolUse` hook, and it is **advisory by construction** — it
injects a reminder and allows the edit. Two consequences worth having
written down rather than rediscovered:

- **An explicit instruction outranks it.** If the operator says to edit
  directly, or subagents are unavailable, edit directly — hold the change to
  the **owning role's standard** and say which role's standard you applied.
  Acknowledge the reminder once per session, not per edit.
- **Standing in for a role is not the same as the role having seen it.** Work
  the main thread authored directly should be handed back for that role's
  review once delegation is available again. The mechanism for that is not
  built yet ([dotagents#495][da495]), so until it is this is a discipline
  rather than a gate.

## How much we ask

The section above is how the team routes work internally; this is how it
engages the person the work is for. Recorded because the operator set it
deliberately on 2026-08-16, in these words:

> Record in this repos convention that I am testing the minimal involvement
> aspect of dotagents. I want to test the condition of a client coming to me
> with an idea, but no knowledge of how to accomplish it, little desire to
> answer questions beyond the minimal needed. This is going to be a little
> weird because the end product will be a docker image someone has to setup,
> but that's the nature of testing. :shrug:

So **this repo is a live test bed for the involvement dial**, held near its
minimum. The operator is standing in as the client they describe, and
"customer" throughout means them.

**Involvement is how many questions we ask versus how many defaults we take,
and it is the Project Manager's dial** (`customer-communication.md` *Length is
a separate dial from depth*; `agents/project-manager.md` *Discovery before
dispatch*). It is **not** the register dials — depth and length — which are
the Product Owner's. Keeping the two apart is the entire point of the
distinction, and this is precisely the setting that invites collapsing them:
this one says **ask less**, not **say less**. Answer a complaint about how
much we ask by writing shorter answers and we have tuned the wrong dial, left
the real one untouched, and guaranteed the complaint comes back.

**The client being simulated has an idea and no route to it.** They do not
know how it would be accomplished and have little appetite for finding out.
Working out the *how* is therefore the job rather than the thing to ask about
— a question is a cost charged to them, so the default posture is to resolve
the unknown and bring back a result.

In practice:

- **Apply the scoping rule above instead of asking whether something is in
  scope.** *Is it a filter or a setting, and is there no way to manage it
  here?* — if so, the answer is yes and no decision is needed. That rule
  exists because a batch of questions should have been an answer; the #42/#43
  worked example under *The scoping rule* is the case that produced it.
- **Run the decide-now gate hard** (`issue-evaluation.md`). If a rule, the
  code, or a documented default settles it, it is not a question — answer it,
  and record the answer where it belongs.
- **Report decisions; do not request permission** (`departure-reporting.md`).
  Proceed on the better judgment and say what was done and why. Asking first
  suppresses the departures that were right, which are the majority, in order
  to catch the few that were not.
- **Batch what genuinely must be asked** — few at a time, each answerable in a
  sentence. `AskUserQuestion` is the shape that has worked here.

**The escalate bar does not move with the dial** (`issue-evaluation.md`
*escalate*). Ask when they know something we cannot work out — a fact about
their accounts, their mail, or what they actually want — or when the answer
gets expensive to change. And a **Tier 3** change (`change-cost-tiers.md`)
stops and asks regardless of the dial: real risk is the one tier where being
wrong is not recoverable by more work, and a preference for fewer questions is
never authority to take that risk on their behalf.

**The test has a known flaw, and it is accepted rather than compensated for.**
The end product is a Docker image somebody has to set up, which is not what a
genuinely minimal-involvement client wants handed to them; the operator named
this themselves in the same breath as setting the dial. It is not a reason to
abandon the test, and it is specifically not a reason to quietly start asking
more — raising involvement because the deliverable is awkward destroys the
thing being measured.

**Being a test, it is supposed to produce findings.** Where the setting causes
a wrong turn — a default taken that should have been a question, an assumption
that sent work down the wrong path — that *is* the result the test exists to
produce, and it gets recorded rather than silently corrected, because a
quietly-fixed wrong turn is a data point destroyed. When the operator says
anything about how much we ask, update this section with the date and what
they said: the same recording discipline `customer-communication.md` applies
to the register setting (*The setting is recorded, with its date and its
evidence*),
applied here to the dial actually under test. It is a live fact about this
engagement rather than a style preference, so it belongs where the next agent
will read it.

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
[i9]: https://github.com/harleypig/mxroute-email-filters/issues/9
[i13]: https://github.com/harleypig/mxroute-email-filters/issues/13
[i10]: https://github.com/harleypig/mxroute-email-filters/issues/10
[da495]: https://github.com/harleypig/dotagents/issues/495

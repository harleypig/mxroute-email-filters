# Releasing

How a version of `mxfilter` is cut — and, first, what "released" does and does
not mean here.

## Nothing is published

`mxfilter` is **not on PyPI, not in any registry, and has no release
pipeline.** There is no publish workflow, no signing setup, and no packaging
step beyond the `setuptools` metadata in `pyproject.toml`. It is installed
from a checkout.

Whether to publish it is an open question nobody has needed answered. That
question is **deliberately left unanswered here** rather than pre-documented:
writing down a release path that does not exist produces instructions nobody
has run, which age into something worse than an honest gap. If publication is
ever adopted it is a decision in its own right — record it as an ADR and add
the concrete steps to this file at that point.

## Installing

From a checkout:

```sh
uv pip install -e .          # editable, into the repo's .venv (development)
uv tool install .            # isolated, on PATH (the uv equivalent of pipx)
pipx install .               # same, via pipx
```

The console entry point is `mxfilter` (`mxfilter.cli:main`), and
`python -m mxfilter` works from a checkout without installing anything.

## Cutting a tag

Versioning is `repo`-scope semver — the full policy is in
[.claude/CONVENTIONS.md](.claude/CONVENTIONS.md) › *Versioning & tagging*. In
short:

- **`v0.y.z` today — alpha.** Per the global `git.md`, `X = 0` means breakage
  is expected and the `y.z` split is loose: `y` for a meaningful addition, `z`
  for a smaller change. Do not agonize over the split at this stage.
- **No API-major alignment.** The sibling provider ties its MAJOR to the
  MXroute REST API's major; this tool speaks ManageSieve and IMAP, which have
  no vendor version to track. Do not import that policy.
- The `0 → 1` jump is a deliberate decision of its own, and is not near.

Steps:

1. Merge the work to `master`.
2. Cut an **annotated** tag at the merge commit and push it — use the
   `release-tag` skill, which automates this:

   ```sh
   git tag -a v0.1.0 -m "v0.1.0"
   git push origin v0.1.0
   ```

3. Move the accumulated [CHANGELOG.md](CHANGELOG.md) entries from
   `## Unreleased` under a `## 0.1.0` heading in the same change.

**A tag publishes nothing**, which is the one place this differs usefully from
the sibling provider: there, pushing a tag triggers a Registry release that
**cannot be unpublished**, so tagging early is dangerous. Here a tag is a
marker on history — cheap, and carrying no outward-facing consequence. It is
still immutable once pushed (`git.md`): a mistake gets a **new** tag, never a
moved one.

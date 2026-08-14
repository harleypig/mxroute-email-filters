default: fmt lint test

# There is no `build` target on purpose: mxfilter is pure Python with only
# setuptools metadata, so there is nothing to compile or bundle (the Build QA
# dimension is N/A — see .claude/CONVENTIONS.md).

venv:
	uv venv

install: venv
	uv pip install -e '.[dev]'

# Format and lint go through pre-commit rather than calling ruff directly, so
# the configs stay the single source of truth for tool version and flags
# (the global rules/pre-commit.md). `fmt` is the modifying prep step; run it
# once, then `lint`.
fmt:
	pre-commit run --all-files --config .pre-commit-config-fix.yaml

# NOTE: this runs the full check config, which includes no-commit-to-branch —
# so it fails on `master` by design. Branch first; that is the convention, not
# a broken target.
lint:
	pre-commit run --all-files

test:
	pytest

# Live tests hit a REAL MXroute account and mutate real state. They need the
# MXROUTE_* credentials in the environment. TESTARGS passes extra flags
# through to pytest, e.g. a run filter for a scoped pass:
#   make testlive TESTARGS='-k sieve'
testlive:
	MXFILTER_LIVE=1 pytest -v $(TESTARGS)

.PHONY: default venv install fmt lint test testlive

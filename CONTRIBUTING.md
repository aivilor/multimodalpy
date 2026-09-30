# Contributing to multimodalpy

Thanks for your interest in contributing! `multimodalpy` is a small, community-friendly scientific Python package, and contributions of all kinds are welcome — bug reports, documentation fixes, new features, and code review.

## Ways to contribute

- **Report a bug or request a feature**: [open an issue](https://github.com/aivilor/multimodalpy/issues) with as much detail as possible (Python version, OS, a minimal reproducible example for bugs).
- **Improve documentation**: typo fixes, clearer examples, and new tutorials are all valuable and don't require deep familiarity with the codebase.
- **Fix a bug or add a feature**: see the development setup below, then open a pull request.
- **Review pull requests**: feedback on open PRs is welcome even if you're not the author.

## Development setup

1. Fork the repository and clone your fork:
   ```bash
   git clone https://github.com/<your-username>/multimodalpy.git
   cd multimodalpy
   ```

2. Create a virtual environment and install the package in editable mode with development dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # on Windows: .venv\Scripts\activate
   pip install -e . --group dev
   ```
   (requires pip ≥ 25.1 for the `--group` flag; alternatively, with [uv](https://docs.astral.sh/uv/): `uv sync --group dev`)

3. Create a branch for your change:
   ```bash
   git checkout -b fix/short-description-of-change
   ```

## Running the checks locally

Before opening a pull request, make sure the full check suite passes — this is the same suite run in CI:

```bash
python -m pytest       # test suite + coverage report
python -m mypy         # static type checking
python -m ruff check . # linting
```

All three must pass cleanly for a PR to be merged.

### Code style

- Code is formatted and linted with [ruff](https://docs.astral.sh/ruff/) (line length 88, numpy-style docstrings).
- Type-check with [mypy](https://mypy-lang.org/); please add type hints to new public functions.
- Docstrings follow the [numpy docstring convention](https://numpydoc.readthedocs.io/en/latest/format.html) (see existing functions in `src/multimodalpy/` for examples).

## Submitting a pull request

1. Push your branch and open a pull request against **`develop`** (not `main`) — this repo develops on `develop` and releases from `main`.
2. Fill in the PR description: what changed and why, and how you tested it.
3. Make sure CI (GitHub Actions `Tests` workflow) passes on your PR.
4. Be responsive to review feedback — most PRs go through at least one round of comments.

## Reporting security issues

Please do not open a public issue for security-sensitive bugs. Instead, email the maintainer directly at aivilor@upv.es.

## Code of Conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md). By participating, you agree to abide by its terms.

## Questions?

If anything here is unclear, open an issue and ask — improving these docs based on real questions is itself a welcome contribution.
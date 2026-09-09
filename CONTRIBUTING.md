# Contributing Guide

This document describes how our team of 3 works together in this repository using a branch-based workflow.

## Branch Structure

```
main
 └── develop (branched from main)
       └── debug (branched from develop)
```

| Branch | Purpose | Who works here |
|---|---|---|
| `main` | Final, production-ready code only | No one pushes directly |
| `develop` | Integration branch where finished features come together | Everyone, via Pull Requests |
| `debug` | Testing/QA stage to catch integration bugs before release | Whoever is testing/fixing bugs |
| `feature/*` | Individual work on a specific task or feature | One person per branch |

## Workflow

```
feature/*  →  develop   (Pull Request, 1+ approval required)
develop    →  debug     (when a batch of features is ready to test)
debug      →  main      (once tested and confirmed stable)
```

**Important:** Never merge `develop` directly into `main`, skipping `debug`. The `debug` stage exists to catch bugs after integration but before release — skipping it defeats its purpose.

## Starting New Work

1. Make sure your local `develop` is up to date:
   ```bash
   git checkout develop
   git pull origin develop
   ```

2. Create a feature branch off `develop`:
   ```bash
   git checkout -b feature/yourname-task
   ```

   **Naming convention:** `feature/name-task` (e.g. `feature/ana-login`, `feature/luis-api`)
   For bug fixes: `bugfix/name-issue`

3. Work, commit, and push regularly:
   ```bash
   git add .
   git commit -m "Describe your change"
   git push -u origin feature/yourname-task
   ```

## Submitting Work

1. Open a Pull Request from your `feature/*` branch **into `develop`**.
2. Request review from at least one teammate.
3. Resolve any merge conflicts before requesting review.
   - Rule of thumb: **whoever opens the PR is responsible for resolving conflicts.**
4. Once approved, merge into `develop`.

## Moving to Debug

When `develop` has a stable batch of features ready to test:

```bash
git checkout debug
git pull origin debug
git merge develop
git push origin debug
```

- Test thoroughly on `debug`.
- Fix any bugs found directly on `debug` (or via short-lived `bugfix/*` branches merged into `debug`).

## Releasing to Main

Once `debug` is confirmed stable:

```bash
git checkout main
git pull origin main
git merge debug
git push origin main
```

Only merge to `main` when the code is fully tested and ready for release.

## Branch Protection Rules (GitHub Settings)

To enforce this workflow, `main` (and ideally `develop`) should have branch protection enabled:

- Require Pull Requests before merging
- Require at least 1 approval
- Require status checks to pass (if CI is set up)
- Disable direct pushes

## General Guidelines

- **Pull before you branch** — always sync with `develop` before starting new work.
- **Keep PRs small** — easier to review with a 3-person team.
- **One feature branch per task** — don't mix unrelated changes.
- **Communicate** — use GitHub Issues or a Project board to track who's working on what, so branches map to tracked tasks.
- **Delete merged feature branches** to keep the branch list clean:
  ```bash
  git branch -d feature/yourname-task
  git push origin --delete feature/yourname-task
  ```

# Testing on real hardware

When your submission PR is merged on the default branch, an **initial benchmark is queued automatically**: the [`submission-merged-benchmark.yml`](../../.github/workflows/submission-merged-benchmark.yml) workflow opens a `test/<owner>/<id>` issue (already labelled `benchmark-request`) for every **new** id in your `submissions/<owner>.json`, and the Pi-Bench daemon runs it on the next polling pass — no manual step needed.

This document is about the **manual path**: how to request a re-run on the Pi-Bench daemon by opening a GitHub issue yourself.

## When to use it

- You merged a submission and want to re-run the benchmark against the **same image** already listed in `submissions/<your-login>.json` (for example, after publishing a new tag, tuning the compose within the rules, or as a sanity check before sponsor week).
- You do **not** use this path to submit new code. Code changes always go through a PR (see [submitting.md](./submitting.md)) — this issue flow only re-triggers a measurement against what is already merged.

## Prerequisites

- `submissions/<your-login>.json` exists on the default branch.
- The `<id>` you want to re-run is one of the `submissions[].id` entries in that file. The format is the same one validated by the PR gate: 1–50 chars, `[a-z0-9._-]`, must not start or end with a separator (see [submitting.md](./submitting.md)).

## How to open the request

Open a new issue with:

- **Title**: exactly `test/<id>` — for example `test/go`, `test/rust`, `test/zig`. The gate validates the title against the regex `^test/[a-z0-9](([a-z0-9._-]{0,48}[a-z0-9])?)$`.
- **Body**: free text; a short note on why you want the re-run is helpful but not required.

A pre-filled issue form is available under **New issue → Benchmark request** (`.github/ISSUE_TEMPLATE/benchmark-request.yml`).

### Maintainers: benchmarking another user's submission

Maintainers (and the `github-actions[bot]` used by the auto-trigger) may target a submission they do **not** own by using an extended title `test/<owner>/<id>` — for example `test/gandarez/go`. The `<owner>` is honored only when the issue author is in the trusted list (configured in [`issue-benchmark-gate.yml`](../../.github/workflows/issue-benchmark-gate.yml) as `TRUSTED_AUTHORS` and in the daemon as `BR_TRUSTED_AUTHORS`). For everyone else, only `test/<id>` (your own submission) is accepted; a `test/<owner>/<id>` from an untrusted author is rejected.

## The label lifecycle

The gate workflow ([`.github/workflows/issue-benchmark-gate.yml`](../../.github/workflows/issue-benchmark-gate.yml)) is the **only** trusted source of the `benchmark-request` label. The Pi-Bench daemon polls for that label, so this is what gates execution:

| Label | Color | Meaning |
|---|---|---|
| `benchmark-request` | green | request validated, queued for the daemon |
| `benchmark-running` | yellow | the Pi-Bench daemon is executing it on the Pi |
| `benchmark-done` | purple | execution finished successfully; results posted on the issue |
| `benchmark-failed` | red | execution started but failed on the Pi |
| `benchmark-rejected` | dark red | the gate rejected the request; the issue is closed |

The gate runs on `opened`, `edited`, and `reopened` — so if you fix the title via edit, validation runs again.

## Why a request can be rejected

The gate rejects with an automatic comment and closes the issue (reason `not planned`) in these cases:

1. **You used `test/<owner>/<id>` but you are not a trusted maintainer.** Only trusted authors may target another user's submission; use `test/<id>` for your own.
2. **The target owner fails the GitHub username format** (`^[A-Za-z0-9][A-Za-z0-9-]{0,38}$`). Extremely rare; only happens for legacy edge cases.
3. **`submissions/<owner>.json` is not on the default branch.** Either no submission PR was opened yet, or it has not been merged. Open a PR first and wait for the merge.
4. **The `<id>` in the title does not exist in `submissions[].id`.** The rejection comment lists the ids actually present in the file — copy one of them and open a new issue.

## Security model

The gate only runs from the default branch and only performs JSON parsing via `gh api`; it never executes code from your submission. The Pi-Bench daemon is the only component that runs the stack, and it does so in an isolated, dedicated environment.

## Known limitations

- There is no documented rate limit for requests; the daemon picks them up by polling, so back-to-back requests on the same `<id>` are serialized.
- Results are posted as a comment on the originating issue and the label transitions to `benchmark-done` / `benchmark-failed`. The exact format of the result comment is owned by the [Pi-Bench daemon](../../README.md) repository and is documented there.

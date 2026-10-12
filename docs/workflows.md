# Evidence-bound workflows

The high-level workflow path is shared by CLI and MCP. It separates successful execution, technical evidence, and explicit artistic acceptance. A plan does not call a provider; execution requires explicit authorization.

## Start with a plan

Run from your project or pass its absolute root **before** the subcommand:

```bash
warden workflow --project PROJECT plan --provider spritefusion --operation generate --prompt "a clockwork robot" --out plan.json
warden workflow --project PROJECT execute plan.json --out art_source/candidates
```

The second command returns `authorization_required` without spending. Add `--authorize-spend` only when you actually want to execute that exact plan:

```bash
warden workflow --project PROJECT execute plan.json --out art_source/candidates --authorize-spend
```

Parameters are an explicit JSON object, for example `--params '{"input_image":"art_source/anchor.png"}'`. Local input and output paths are confined to the project root. Credentials and inline image bodies do not belong in plans; use provider authentication configuration.

### Strict prompt sanitization

Planning and event capture share bounded structural inspection of embedded JSON, including escaped keys and nested serialized values. Credential-bearing plans are rejected before persistence. Malformed JSON-like fragments are intentionally treated conservatively: even harmless text such as `draw [idle] and [walk] poses`, `draw a 6" robot`, or `draw a {blue|red} cape` can be rejected. Prefer plain wording such as `draw idle and walk poses`, `draw a six-inch robot`, and `draw a blue or red cape`. Safe-text preservation is not universal; do not bypass sanitization to work around a rejection.

A plan binds the exact operation, registry hash, input-file hashes, discovered pipeline, and project configuration. Changed dependencies require a fresh plan. Execution records a durable unique claim before dispatch. Repeated execution returns the existing state rather than resubmitting a paid job. A crash or uncertain provider completion is **not** permission to retry: inspect the receipt and provider job history first. Outputs go in a new per-plan candidate directory, never directly over runtime art.

Some documented operations remain advisory or import-only when Warden has no executable adapter. Such operations must be rejected explicitly; a generic endpoint or fetch of an existing asset is not a substitute for a native character workflow. Provider success is not artistic approval.

## Evidence and recovery

```bash
warden workflow --project PROJECT history --limit 100
warden workflow --project PROJECT history --run-id PLAN_ID
warden workflow --project PROJECT metrics
warden workflow --project PROJECT replay-spool
```

The append-only SQLite ledger defaults to `PROJECT/.spritewarden/events.db`. Override with `SPRITEWARDEN_EVENTS_DB`. Spool receipts live beside it in `spool/`, or at `SPRITEWARDEN_EVENT_SPOOL`.

Capture sanitizes authorization, credential/token fields, URL userinfo/query/fragment, and image bodies before persistence. Capture failures produce a warning and attempt an atomic sanitized spool file. An initial intent that cannot reach the primary ledger must not proceed to spending. Replay recovers ledger/learning receipts; it **never reruns generation**.

Judgments, attempts, and explicit decisions project idempotently into the learning DB (`SPRITEWARDEN_LEARNING_DB`, otherwise the existing Warden default). Technical checks and provider completion remain `pending` artistic outcomes. Only explicit acceptance/rejection decisions supply artistic outcomes. Projection failure is visible and recoverable through the spool.

Metrics separate unavailable checks from visual failures and report review-disagreement **candidates**, not proven checker mistakes. Unknown cost stays unknown. Cost-per-accepted-output is withheld when coverage is incomplete. The behavioral corpus in `tests/fixtures/workflow_cases.json` retains the distinction between observation, scoped exception, stale evidence, and retracted diagnosis; it is not a claim of live provider quality benchmarking.

## Import historical observations safely

```bash
warden workflow --project PROJECT backfill --rejections art_source/rejections.jsonl
warden workflow --project PROJECT backfill --rejections art_source/rejections.jsonl --apply
```

Default is a preview. Applying is idempotent for identical source lines. A historical pipeline-attributed provider is retained as `reported_provider`, not asserted to be the actual generator. Missing prompts, requests, costs, approvals, and success denominators are not reconstructed. The import does not promote art, modify sidecars, or manufacture learning confirmations.

## Operational boundaries

- A Claude hook only governs recognized calls delivered through that host's hook integration. Other hosts and wrapped scripts are not automatically intercepted.
- Use shared plan/execute for governed operations. Keep native provider MCP workflows explicit where no Warden adapter exists.
- Do not use `force_ungated` as artistic approval or bypass a failed contract.
- Failed candidates are inspectable. Promotion/integration—not viewing diagnostic evidence—is the strict transition.
- Existing project state and live Tipshare tips are not migrated merely by installing this code. Review migration and correction proposals before applying them.
- Back up the project and knowledge databases before deliberate migration. No migration should overwrite a current artistic choice inferred only from contradictory prose.

## Tests

```bash
.venv/Scripts/python.exe -m pytest -q
```

`tests/conftest.py` isolates event, learning, and spool locations for each test. Provider execution tests use mocked transport or drivers. No test requires generation credits. On Windows invoke the venv interpreter directly rather than relying on a mismatched `pip` or `uv` console trampoline.

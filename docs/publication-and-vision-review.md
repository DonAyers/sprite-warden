# PixelLab replacement and stable vision review

## Review locally before publishing

`warden publish --project PROJECT review --views art_source/character/idle_fixed --out .spritewarden/review/current.png`

Produces a neutral-background contact sheet and `current.png.json` with full original findings and readiness. No model requests. Existing outputs are never overwritten. Stills above 384 px should be inspected in the gallery instead.

Read existing `review-check` evidence first. Repeatedly invoking a vision model is not a way to discover a consensus approval. Neither passing pixel checks nor publication authorization waives artistic failures.

## Native rotation replacement

Provider source: official PixelLab MCP guide, `save_to_asset` operation. This operation replaces remote content; it is not generation. Warden currently validates **registered character rotation targets using explicit upload UUIDs only**. Object, animation-group and gallery targets, base64, job/workbench references and arbitrary image URLs remain unsupported here. Native MCP is required; no Warden provider adapter or guessed REST endpoint is added.

1. Obtain explicit owner authorization for the particular replacement after reviewing its local evidence. Use `warden accept` only for owner-approved existing artistic findings, never pixel failures or missing evidence.
2. Through the native provider client, upload the exact ready PNG. If the provider supports upload downloads, download that upload to a separate project-local downloads folder. Preserve the remote original for rollback. Do not add readback PNGs into the direction-set folder: membership changes invalidate evidence.
3. Prepare an authorization, with the actual references returned by the provider:

```sh
warden publish --project PROJECT prepare --source art_source/character/idle_fixed/east.png --image upload:UPLOAD_UUID --target character:CHARACTER_UUID:east --uploaded-file downloads/upload-east.png --by OWNER --reason "Owner's actual replacement authorization"
```

The source must be ready under existing gates and revision authority. Its recorded facing must match the target; its canvas must match the registered target. When supplied, uploaded readback pixels must equal the source. If the provider has no upload download, use `--no-upload-readback "<provider: reason>"` instead of `--uploaded-file`, for example `--no-upload-readback "pixellab: no download endpoint for uploads"`. This records `uploaded_readback: unavailable` and the provider gap; only the pre-save upload comparison is deferred. The pixel check remains mandatory at `confirm` on a fresh saved-rotation download and at `verify-character` on the complete character download. Never substitute the local PNG as a remote readback. Exactly one readback mode is required. The receipt binds image reference, target, exact source/evidence, registered target bytes, owner attribution and reason; expires after 24 hours; and requires a matching durable ledger event. It is not a remote request. A hook override cannot bypass publication validation.

4. Run `warden check-call` for the exact native `save_to_asset(image=..., target=...)` call, then use the native MCP tool only after allow. Other generation rules are unchanged.
5. Retrieve the updated character through the native client and download the saved rotation into a separate downloads folder, bypassing stale HTTP caches as supported by the provider. Confirm:

```sh
warden publish --project PROJECT confirm RECEIPT_UUID --readback downloads/saved-east.png
```

Confirmation compares pixels and emits a `publication_readback` receipt. It closes authorization against reuse. Expired authorizations cannot permit a save, but an unchanged completed replacement may still be confirmed from readback. If a native call has uncertain completion, inspect/read back before any retry. Do not update local registered target bytes before confirmation because doing so invalidates the prepared target snapshot.

6. After the replacements, download the complete current rotations through the native client and verify every registered facing, not just the two repaired uploads:

```sh
warden publish --project PROJECT verify-character CHARACTER_UUID --download-dir downloads/current-character/rotations
```

The locally registered set must be ready and must match all remote rotation pixels. This catches palette-snapped local sets whose other remote rotations are still the original colors. After publication opts a character into this workflow, native character animation/state calls require this complete readback receipt; overrides cannot bypass it. Pending replacement authorizations must first be confirmed or revoked. The receipt binds local bytes/evidence and publication state, expires after six hours, and is invalidated by a later failed/incomplete readback or local/publication changes. Unmodified legacy registrations remain compatible. External provider/UI changes cannot be detected offline: always obtain fresh native downloads after external edits, and immediately before starting a generation batch.

Unused, stale, or uncertain local authorization can be explicitly revoked:

```sh
warden publish --project PROJECT cancel RECEIPT_UUID --by OWNER --reason "Do not use this replacement authorization"
```

Cancellation is not remote rollback. Full character readback is still required to establish what is actually stored before generation.

**Trust boundary:** CLI `--by` is attribution, not authentication. Warden does not authenticate PixelLab ownership or fetch upload/remote bytes itself. The operator/client must supply honest downloads from the named upload and exact saved target. Supplying the original local PNG as if it were a remote download does not establish remote verification. This local contract does not claim cryptographic provider attestation. A receipt may allow the same exact native arguments until confirmed or expired; it is not an exactly-once provider transaction.

## Stable reference comparisons

Completed match AND mismatch results are cached by exact candidate/reference pixels, model, comparison inputs and vision implementation fingerprint. Unavailable/error results are never reusable success. Cache lives at `.spritewarden/vision-cache` under the judgment project context (or current directory for direct library use); override with `SPRITEWARDEN_VISION_CACHE`.

The default result is immutable. Identical requests reuse it without another model call. Changed pixels, description, facing, perspective, face descriptions or model produce a different key. Identical gate rejudgments preserve the sidecar and existing exact-evidence acceptance instead of invalidating it through a new timestamp/event ID.

Explicit paid rechecks are **report-only**, preserve the default and append history:

```sh
warden judge-views VIEWS --vision --refresh-vision --no-record --json
```

MCP: `judge_direction_set(..., vision=true, refresh_vision=true, write_gate_record=false)`.

Rechecking does not replace existing gate records or human acceptance. Inspect disagreement rather than treating the latest random verdict as more authoritative. Historical records without the new exact request provenance are not silently imported into the cache; use existing gate records without rerunning vision when no new inspection is needed.

In-flight locks prevent concurrent duplicate comparisons; interrupted locks require explicit investigation/removal after establishing no request is active. No blind automatic retry occurs. Failed initial intent capture prevents a new vision call. Cached original cost describes its original evidence; `vision.reused` events report zero new comparison cost.

# Bildbot vision-reference investigation

## Scope and result

Investigation branch: `fix/vision-reference-face-authority`. This report records the pre-integration proposal; commit/merge status is tracked by Git rather than this historical snapshot. Existing dirty-tree work was preserved. The game project was treated as read-only. No provider vision/generation request, override, acceptance, art edit, pipeline edit, or STATUS edit was performed by this investigation.

**Conclusion:** this gate is useful as a fallible review assistant, not a trustworthy sole artistic approval authority for this turnaround. Its original prompt contained contradictory instructions and historical-text contamination. The parser also accepted incomplete JSON objects as clean comparisons. Prompt fixes and explicit sampling are not proof that the model now sees the artwork correctly: all verification was offline.

Line references below marked **before** refer to the saved pre-investigation source snapshot, not necessarily the current branch line numbers. The standalone patch contains only this investigation's delta relative to that snapshot, not earlier uncommitted repair/publication work.

## 1. Request composition and offline reconstruction

- MCP: `src/spritewarden/server/mcp_server.py:363-394`. Identity comes from `character_id`. Anchor precedence is an explicit `anchor_path`, then an existing identity turnaround still for the anchor facing, then that facing in the loaded set. When the set itself supplies the anchor it is skipped as a comparison target. The pipeline's historical `anchor` field is not automatically the vision anchor in this path.
- The nearest pipeline is loaded at lines 386-389. **MCP does forward `faces`**, not just CLI. Subject precedence at line 392 is explicit `description`, then pipeline `description`, then identity `prompt_description`. These are alternatives, not concatenated descriptions. Updating identity text cannot fix a higher-priority conflicting pipeline description.
- CLI: `src/spritewarden/cli_full.py:387-416`: similar anchor selection; optional supplied `faces`, otherwise nearest pipeline faces; explicit `desc`, then pipeline description, then identity description. CLI passes its `perspective` argument. MCP currently uses `compare_views_to_anchor`'s default `low top-down (approx 20-30 degrees)` rather than an exposed direction-set perspective argument.
- `src/spritewarden/judge/views.py:314-351` canonicalizes facing names and forwards description, perspective, faces, and refresh to each comparison.
- **Before** `judge/vision.py:571-582`: one user message containing text FIRST, then ONE image. That image is a labelled ANCHOR/CANDIDATE composite, reference on the left, candidate on the right. `prepare_pair_for_vision:253-278` alpha-crops each image, enlarges nearest-neighbour at zoom 6, aligns baselines and labels them. Background is `(109,161,138)` at line 26, not black. This visual cropping does not edit the source PNG.
- **Before** `reference_prompt:315-349`: subject/camera, turn/occlusion instruction, declared faces, numbered questions, and JSON schema.

Artifacts saved outside the game project:

- `run3-north-prompt-before.txt`: the complete pre-fix prompt reconstructed offline for north.
- `run3-north-request-board.png`: the reconstructed image board.
- `reconstruction.json`: parameters, computed key, and provenance limitations.

The opening north record's pipeline SHA-256 equalled the pipeline SHA-256 at reconstruction (`idle_fixed2/north.png.gate.json:78-81` in the opening snapshot). Thus the pipeline contents used for reconstruction are the recorded dependency version, not a newly invented historical description. **However, the record has no non-null request fingerprint and no captured request body.** The artifact reconstructs the pre-fix code path; it is NOT proof of the exact bytes sent by a particular long-running MCP process. Historical explicit-description overrides and loaded-code version were not fully recorded. We do not claim a captured wire request exists.

## 2. Why declared back details are still called invented

Two proven prompt defects:

1. **Before** `judge/vision.py:296-305` included every faces key. The live `pipeline.json:95` has `_about` containing the obsolete “plain… no bolt, no boss” history. It was sent as part of the *approved whole design*, alongside the new positive back description at lines 96-98. Historical commentary was mixed with current design constraints.
2. **Before** `judge/vision.py:324-327` subsequently instructed that a boss or bolt on the back/sides of the head absent from the anchor's head is invented. This conflicts with treating the unseen-face description as design authority.

The wording gives the anchor conceptual precedence, not a numeric model weight. There is no API parameter that weights anchor versus text. Also, “no counterpart on the anchor's design” is appended by `ReferenceComparison.failures()` (**before** `vision.py:118`) to every invented-part finding. That phrase in the gate is not necessarily the model's exact wording or proof that it considered and rejected the face description.

**Patch:** remove underscore-prefixed metadata from effective face input and cache identity; state explicit unseen-face authority; remove the anchor-only bolt rule; require positive visible contradiction rather than infer blank unseen surfaces. No automatic noun-matching waiver is added: merely mentioning bolts must not silently approve every extra component.

## 3. Missing versus hidden details

The original gate does **not** calculate geometric visibility, ray-cast a model, or know which corner bolt is behind a silhouette. The model produces `missing_visible_parts`. `parse_reference_response()` (**before** `vision.py:176-182`) applies `_OCCLUSION_RE` at lines 67-70 to move some self-described occlusions to `expected_hidden_parts`. This is a text heuristic, not geometry.

“Should be visible from east” is also appended locally by `failures()` (**before** `vision.py:119-120`); it does not certify geometric visibility. In a northwest view, demanding all four rear corner bolts is likewise unsupported without visibility evidence.

**Patch:** conservative prompt guidance distinguishes back visible from north, partially visible on rear diagonals, hidden from front facings, and edge-on/occluded in profiles. It explicitly says far corners may be occluded and a complete rear layout must not be demanded in profile. Surface decoration is distinguished from protruding limbs/tools. Left/right physical surfaces must not be silently equated with screen-left/screen-right. This remains guidance, not a visibility measurement or automatic acceptance rule.

## 4. Sampling, repeatability, and cache

**Before** `_chat_json()` (`vision.py:455-459`) supplied model, messages and `response_format=json_object`, but no temperature, top_p, seed, or explicit deterministic parameters. The provider's sampling defaults are not recorded here. Different replies are possible; byte-identical PNGs alone do not prove identical full requests.

The SHA checks confirmed:

- Run 1 versus run 2: six unchanged files; east/west changed.
- Run 2 versus run 3: south/east/north unchanged; five others changed.
- Run 3 also changed the declared face text. A cache miss for a genuinely changed text request is correct even if a candidate PNG is unchanged.

The completed run-3 records lack non-null request fingerprints. A completed result passing through the current cache-producing wrapper should have one. This is evidence that these records do not demonstrate current caching was active; stale loaded MCP code or another old execution path is plausible, not proven. An editable install does not reload already imported Python modules. Reload the server only when intentionally activating a reviewed change, and check provenance rather than assuming a CLI update refreshed the server.

**Before** `judge/reference_cache.py:13-19` keyed decoded candidate/reference pixels, facing/anchor facing, description, perspective, zoom, faces, actual prompt SHA, prepared-board digest, model and the vision module source hash. Changed identity text matters only if selected into the actual description. Defaults are immutable; explicit refresh preserves the default and is report-only at the CLI/MCP boundary.

**Patch:** temperature 0 and top_p 1 are explicit. No unsupported seed is invented. These reduce sampling variation but do NOT guarantee identical replies across provider/model/hardware changes. The key now includes actual sampling and response format. Underscore metadata no longer causes an unnecessary comparison miss. Cache/readback audit includes sanitized effective parameters, prompt SHA, board digest, model, sampling and implementation hash. Raw prompts are not placed in general telemetry; reconstruction can be verified from matching code/parameters and hashes where sanitization has not removed private fields.

Old records are not retroactively assigned fabricated cache provenance or cherry-picked historical passes. Changing this prompt/implementation legitimately creates a new request key, but this investigation did not make that new paid request.

## 5. West's broken JSON

**Before** `vision.py:470-475` used strict `json.loads` once, with no JSON repair and no retry. `response_format=json_object` did not prevent a truncated reply. `_compare_to_reference:584-585` returned `status=error`. `reference_gate:362-367` set passed false with metrics status error. `gates_from_scorecard` (`gates/records.py:70`) collapsed both error and unavailable into top-level unavailable. The set summary (`judge/views.py:355-368`) then described all nonpassing views as differing from the anchor, hiding the distinction.

The existing live west record therefore already carries **error** in metrics, not a completed artistic mismatch. `gates/acceptance.py:25-31` refuses it. That refusal is correct: accepting an error would falsely claim a completed assessment. We did not invoke acceptance on game evidence.

Another real bug reproduced offline: `{}` or an object without critical answer fields was parsed into an empty comparison and could become **match**.

**Patch:** preserve top-level error and label it “not assessed”; separate unassessed views from artistic mismatches in the aggregate; reject missing/type-invalid invented/missing lists or same-character boolean as invalid schema. Extract reported usage cost before parsing so a charged malformed reply does not lose its reported cost. Error attempts retain sanitized audit/history and failure events but never become reusable completed defaults. No automatic paid retry or guessed repair of truncated content is introduced.

## 6. Failure granularity and severity

`ReferenceComparison.failures()` (**before** `vision.py:116-134`) treats every named invented/missing part as a failure. The `PartNote.size` field is free text (**before** lines 47-62), not a measured pixel area or severity threshold. There is no small-part exemption.

**Before** `reference_gate:404` scores `max(0,1 - 0.25*error_count)`, not a calibrated probability. A single finding is normally 0.75, not 0.0; four or more errors floor the score at zero. The recorded runs demonstrate both. Every error still makes the view nonpassing.

This is a strict policy choice, not an arithmetic bug. Parts genuinely authorized by the applicable face design should not be invented findings; the contradictory prompt was a defect. Blindly ignoring anything labelled “small” would also ignore real unwanted lamps/parts and is not an acceptable fix. Preserve findings and use explicit owner review of completed disagreements. No global severity threshold or blanket acceptance is added.

## 7. Content-height policy versus canvas

`judge/start_image.py:24-31` defines DRAWN-height ranges: `48-64` = 40–64px; `96` = 72–96px. `start_image.py:194-210` precedence is explicit target height, explicit tier, identity tier, then an identity-cell fallback range. The 96×96 canvas is a different field from a 59px drawn robot.

The live identity snapshot (`idle_fixed2/north.png.gate.json:27-30,47`) declares both canvas dimensions 96×96 and content tier 96. Pipeline `art_source/bots/bildbot/pipeline.json:6` declares tier 48-64. `cli_full.py:699-703` explicitly passes that pipeline tier during `stage anchor`; direct CLI (`cli_full.py:238-240`) and MCP (`mcp_server.py:281-283`) pass their supplied tier, otherwise the low-level gate falls back to the identity. This explains the conflicting results.

**Diagnosis:** the identity's content-tier value is misconfigured relative to the documented intended drawn size; the content-height check itself is correct. Do not infer content tier from canvas size or silently widen the range. Proposed follow-up, NOT applied: align bildbot identity `size_tier` to `48-64`, retain `dimensions=[96,96]`, and keep the pipeline consistent. Until explicitly corrected, callers can specify the declared `48-64` tier. Other gates (including symmetry) remain independent and may still fail. An offline 96-canvas/59-content fixture reproduces the wrong-tier failure and explicit-tier content-height pass.

## Owner workflow and limits

1. Keep deterministic pixel checks as hard requirements. Do not regenerate art because the critic contradicts itself.
2. Review current neutral pairs/contact sheets and the current face descriptions. Do not use old prohibitions or metadata as current requirements.
3. For completed, current artistic mismatches, the owner may approve specific exact-evidence views or request deterministic repairs. This investigation records no approval.
4. Error/unavailable views remain unassessed. `warden accept` cannot waive them. Independent owner visual-review evidence would need its own explicit supported mechanism; pretending an error is a model mismatch is not that mechanism. No paid recheck is authorized by this report.
5. The pipeline records a real rejected north-arm orientation defect independently of the critic (`pipeline.json:40`). Do not bury that defect under a blanket acceptance or publish the unresolved set merely because the wording changed.
6. Keep remote publication separate from art review. Existing upload/target authorization and complete-character pixel readback still apply. A cached opinion is not provider synchronization proof.

## Verification

`SPRITEWARDEN_OFFLINE=1 SPRITEWARDEN_BROWSER_TESTS=1 ... -m pytest`: **632 passed, 2 skipped**, including existing browser tests. New regressions cover face authority/metadata, visible/partial/hidden instructions, actual request sampling, malformed JSON cost/no retry, error serialization, effective-request audit/cache reuse, non-reusable error attempts, aggregate classification, MCP forwarding, incomplete response rejection, and content-height semantics.

The final comparison found **no changed PNG artwork**, but eight `idle_fixed2` gate records, the pipeline and STATUS changed concurrently. The newer records are timestamped 18:59:15 UTC, omit vision evidence, and the new pipeline note reports a separate owner-directed no-vision re-gating action. This session did not execute that action or edit/restore those files. The diagnosis refers to the opening 18:04:20 UTC evidence snapshot; the attached `game-verification.json` records the changes rather than falsely asserting a fully unchanged live project. No merge or provider operation occurred here.

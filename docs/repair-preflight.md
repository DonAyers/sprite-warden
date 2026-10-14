# Repair-plan preflight

Use this **before** drawing or requesting a provider repair. It distinguishes a color defect from declared geometry/orientation or an unresolved assessment. It measures actual PNG candidates and masks. It does not draw, accept, publish or submit generation.

## Entry points

```bash
warden repair-plan --project PROJECT --plan repair-plan.json --html NEW-report.html
```

MCP: `preflight_repair(project_root, plan, ask_jev=false, authorize_decision_spend=false)`.
Restart/reconnect the MCP server after updating the editable install.

Default inspection is free and does not write project state. HTML is an optional **new** file at the explicitly supplied path; existing files are never overwritten. The result always has `execution_authorized: false`.

## Example geometry proposal

```json
{
  "source": "art_source/character/north.png",
  "goal": "Robot faces away. Reconstruct the elbow and cannon so the open muzzle is not presented toward the viewer.",
  "defect_kind": "orientation",
  "proposed_action": "geometry_patch",
  "required_regions": {"elbow": [40, 44, 20, 10]},
  "preserved_regions": [],
  "available_actions": ["geometry_patch", "donor_patch", "regenerate_direction", "ask_owner"],
  "spend_cap_usd": 0.0,
  "quoted_costs": {}
}
```

Coordinates are **example coordinates**, not an inferred location in your sprite. Supply actual `[x,y,width,height]` rectangles within the canvas. Adding `candidate` measures pixel changes inside each required region; adding `mask` measures selected pixels and existing opaque source pixels within it. All images must be PNG sprites up to 512×512, under the resolved project root. Path traversal and symlinks escaping it are rejected.

## Deterministic checks

- Exclude `color_patch` for declared geometry/orientation defects.
- Block a plan that freezes a required region.
- Block a candidate that changes none of a required region's pixels, or is pixel-identical overall.
- Block empty masks; warn, rather than automatically reject, a mask containing no existing opaque source pixels.
- For assessment disputes/unknown defects, only `ask_owner` is eligible; a model disagreement is not permission to alter art.
- Exclude paid generation without a quote within a positive supplied spend cap.
- Exclude another identical request after a recorded no-op.
- Stop at the declared paid-attempt limit (default one, configurable up to three).

`available_actions`, quotes and historical attempts are **caller-supplied planning facts**, not authenticated provider capability or billing receipts. An eligible strategy is an option to discuss, NOT a compiled executable provider request. Before execution use the existing provider contract/workflow and its authorization checks. This preflight is an advisory MCP/CLI entry point, not a mandatory interception of every external provider call.

For no-op history, supply `request_fingerprint` and `attempts`:

```json
{
  "action": "inpaint",
  "request_fingerprint": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "changed_pixels": 0
}
```

The fingerprint must represent the exact native request including source/mask identities and generation arguments. The string above is a **placeholder**, not a real request hash. Do not use an arbitrary retry identifier as if it bound the request. Unknown history cannot prove an attempted request was different.

## Optional Jev advice

```bash
warden repair-plan --project PROJECT --plan repair-plan.json --ask-jev --authorize-decision-spend --html NEW-report.html
```

The two flags authorize **one Jev decision query only**, never generation, artistic acceptance or publication. This is an explicit authorization for a potentially billed decision call, not a dollar-guaranteed API cap. Generation quotes/spend cap do not apply to Jev billing. Context is bounded to 16,000 characters, timeout is 10 seconds, no automatic retries. Requires OpenRouter credentials and online mode. Blocked plans never reach the model.

The existing OpenRouter Decisions API client asks Choice over only the remaining strategies. The state includes goal, declared defect, measured pixel facts, region constraints, attempt history and quote/cap facts. No image bytes or credentials are in the decision state. Model confidence is optional and **not** calibrated safety. A missing/invalid/unavailable answer remains unknown; no invented affirmative fallback or confidence.

A durable `repair.decision_started` event is required before the query; completion records the typed outcome and reported cost. If intent cannot commit, no query is submitted. A lost completion receipt must not cause a paid retry; consult the durable intent/receipts. Offline inspection does not create these events.

## Donor orientation and pose review

For geometry/orientation plans, a `donor_patch` now requires `target_facing` and an actual `donor` object:

```json
{
  "target_facing": "north",
  "donor": {
    "image": "art_source/character/south.png",
    "facing": "south",
    "region": [67, 44, 11, 21],
    "usage": "direct_copy",
    "transforms": ["flip_horizontal", "translate"]
  },
  "references": [{"image": "art_source/character/east.png", "facing": "east"}]
}
```

This is an **illustrative blocked operation**, not a recommended repair or a complete plan. The region is an example and must fit the actual donor canvas. Canonical facings use hyphens: `north-east`, `south-east`, `south-west`, `north-west` (plus cardinal names). No alias normalization or facing inference from filenames occurs.

A direct geometry/orientation copy across different declared facings is excluded: reflection/translation is not a reconstruction of visible surfaces and depth. Relabeling such a copy `geometry_patch` does not clear its blocker. `redraw` is not a direct-copy transformation. To use a front view as a design reference, set `usage: reference`, `transforms: [redraw]`, choose `geometry_patch`, and actually reconstruct the pose against the target view. Missing donor details exclude that strategy before optional Jev ranking even when another action was proposed.

These are conservative checks on **declared provenance**, not a visual proof or authentication of labels or claimed operations. Misdeclared/omitted donor use and external scripts remain outside this guarantee. Color-only donor work does not imply pose correction. Valid same-facing copies are still pose-unverified.

`mechanical_status` and `pose_review_status` are separate. Geometry/orientation reports always show **Pose: UNVERIFIED**, even with clear mechanical checks. The report shows whole original/candidate, donor and optional direction references at game scale and zoom; image bytes must match the measured SHA-256 snapshots. Show the rough pose to the owner before polishing or paid validation. No new model or automatic pose-approval gate is introduced.

## Saved repair binding

`repair.save_candidate(..., repair_plan=plan)` and gallery `/api/repair` payload field `repair_plan` attach the canonical plan to a new candidate. Plans must resolve to the actual source, use a local patch strategy, and must not name a separate `candidate`: the save path checks the actual computed candidate pixels in memory. Blocked plans never save. Donor/reference/source hashes are rechecked after durable intent, before writing. `repair.json` and catalog provenance retain plan, donor/reference identities, mechanical findings, candidate hash and unverified pose state.

Alpha/silhouette changes require an explicit geometry/orientation plan. Legacy RGB-only work without a plan remains supported but is marked **preflight not supplied / pose not assessed**; its semantic intent cannot be inferred from pixel changes. Attach a plan for all pose repairs, including RGB-only internal-geometry edits. The gallery provides a plan JSON editor and displays the separate mechanical/pose statuses after saving. Existing source-bound gate/owner acceptance rules still apply; saving does not confer pose approval or inherit an older candidate's approval. This integration does not intercept external provider requests or arbitrary file writes.

## Human boundary

Show the original, candidate, measurements, blockers, strategy options and any Jev recommendation. Have the owner inspect the actual pose at game scale and zoom. Changing pixels in an elbow region does **not** establish that the elbow looks correct. Filename `north` and preserved alpha are not visual evidence of away-facing orientation.

Missing Jev confidence is not defaulted to 1.0. Existing offline production orchestration now reports `decision_status: unknown`, `recommended_action: review`, and does not pass solely through a fabricated Jev result. Zero readiness/confidence fields in this compatibility output are unknown sentinels, not measured probabilities.

CLI exit code 2 means a blocked/error plan; the JSON explains why. A proposal exit code 0 is not approval. Reconnect MCP and call `preflight_repair` before the next repair. First run without Jev, fix deterministic blockers, and show the plan to the owner before requesting spending authorization.

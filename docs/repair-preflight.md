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

## Human boundary

Show the original, candidate, measurements, blockers, strategy options and any Jev recommendation. Have the owner inspect the actual pose at game scale and zoom. Changing pixels in an elbow region does **not** establish that the elbow looks correct. Filename `north` and preserved alpha are not visual evidence of away-facing orientation.

Missing Jev confidence is not defaulted to 1.0. Existing offline production orchestration now reports `decision_status: unknown`, `recommended_action: review`, and does not pass solely through a fabricated Jev result. Zero readiness/confidence fields in this compatibility output are unknown sentinels, not measured probabilities.

CLI exit code 2 means a blocked/error plan; the JSON explains why. A proposal exit code 0 is not approval. Reconnect MCP and call `preflight_repair` before the next repair. First run without Jev, fix deterministic blockers, and show the plan to the owner before requesting spending authorization.

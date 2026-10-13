# Human visual review and artistic exceptions

The gallery `/review` is the primary human review surface: show the image, each
original pixel/vision finding, and the person's accept/reject rationale. The CLI
is a secondary entry point to the same acceptance backend. This change does not
add gallery controls or grant an agent authority to approve an owner's artwork.

```console
warden accept IMAGE_OR_DIR --by "Reviewer name" --reason "Specific visual review rationale" --gate vision_reference
warden review-check IMAGE_OR_DIR --require views --json
```

Only completed, failed `vision_reference` evidence can be excepted. The default
and only CLI scope is that named artistic gate. Missing/unavailable/error evidence,
human rejection, changed bytes/dependencies, and pixel failures (including the
`direction_set` pixel measurement) cannot be waived. Passing directory members
are validated but not rewritten. The original `gates` map is preserved.

The decision binds SHA256, the entire evidence snapshot, source/reference,
identity, pipeline and direction-set membership dependencies. Changing those inputs
invalidates an exception. An exact, unchanged rejudgment can reuse the original
record and preserve its acceptance; an explicit vision refresh is report-only.
When evidence actually changes, have the human review it again; do not infer a
new approval from an old rationale.

## Reusable backend for gallery integration

* `spritewarden.gates.acceptance.accept(paths, *, actor, reason, gate='vision_reference')`
  validates the complete target before writes and returns decision objects. It
  raises `ValueError` for policy/staleness conflicts and `RuntimeError` when durable
  audit intent is unavailable. A gallery caller must enforce its existing CSRF,
  same-origin, loopback and explicit-human-review controls before calling it.
* `spritewarden.gates.records.stage_readiness(image, stage)` includes `acceptance`
  (decision ID, actor, reason, time, scope) alongside original `record`,
  `failed_checks`, `unavailable_checks` and blockers. Measurements remain failed;
  transition readiness can be satisfied by a valid scoped human exception.
* `require_passed(...)/review-check --json` includes `accepted` entries.
  `passed_gates` returns effective satisfied names, not rewritten measurements.
* `active(image, record)` resolves the current valid artistic exception or `None`.

For revisioned pipelines, acceptance calls the existing
`workflow_state.record_decision(..., decision='accepted_with_exception',
gate='vision_reference', operation=<exact revision operation>)`. It does not
modify sidecars, infer migration, select/promote a revision, or revive rejection.
The command must target all artifacts of one exact registered revision; the
normal explicit promotion step remains required for spending. Gallery callers
with revision/version handles can use `record_decision` directly for optimistic
concurrency. Legacy pipelines keep the same decision semantics in `human_decision`
on the failed image sidecars, with an evidence fingerprint. Revision state always
wins once present; a legacy sidecar cannot bypass it.

`human.decision.intent` must be durably recorded before acceptance writes;
`human.decision` records completion. Named actor, reason, time and exact scope
are retained. The CLI is attribution, not authentication: entering a person's
name is not proof that person approved. Agents must not invent or paraphrase an
owner's approval into a live decision.

Existing revision exception scopes (`symmetry`, `loop_smoothness`,
`walk_mechanics`, `direction_set`) predate this change. They are **not** exposed by
this CLI/backend. In particular the new vision path never waives the pixel
`direction_set` result. A broader review of those older policies is separate work.

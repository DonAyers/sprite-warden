# Explicit thin-profile direction sets

The default gate is unchanged: every facing's opaque area must be within
`area_tolerance` (default 0.20) of the anchor. A narrow character may explicitly
opt into `CharacterIdentity.side_front_area_ratio = [minimum, maximum]`.
Both bounds must be finite, with `0 < minimum <= maximum <= 1`.

For a thin-profile robot, configure the catalog used by your project (set `SPRITEWARDEN_DB`
to a scratch/pilot catalog when experimenting, not the live catalog):

```sh
warden identity set example-bot --desc "Example wheeled builder robot" --side-front-area-ratio 0.60 0.75
warden judge-views "PATH/art_source/bots/example-bot/character-2026-10-11/idle" --char-id example-bot --area-tolerance 0.20 --no-record
```

An existing identity retains its other fields; `--desc` can be omitted when
updating it. Do not widen the global area tolerance to accept narrow sides.

MCP uses identical semantics:
`register_character_identity(id="example-bot", name="Example", prompt_description="Example wheeled builder robot", side_front_area_ratio=[0.60, 0.75])`,
then `judge_direction_set(source_path=".../idle", character_id="example-bot", area_tolerance=0.20, write_gate_record=False)`.

With this policy:
- Nonempty south, east and west views are required. South is FRONT for area,
  even if the supplied anchor is east or west.
- External anchors are never omitted from area checking. Their facing defaults
  to south; front/back/diagonal anchors must agree with south within the ordinary
  area tolerance. Explicit east/west anchors must have anchor/south area within
  the calibrated profile bounds. Use CLI `--anchor-facing east` or MCP
  `anchor_facing="east"` with a side anchor. A globally half-sized set fails
  against its external full-body anchor even when its internal ratios pass.
- Each east/west area divided by south area must lie within the explicit bounds.
- East and west must agree symmetrically: `max(area)/min(area) - 1 <= area_tolerance`.
- North and all diagonals retain the ordinary area tolerance against FRONT.
- Ground row, height, canvas, connected components, tool markers and palette
  checks are unchanged. Empty bodies and half-height views still fail.

CLI and MCP gate records capture the policy, area tolerance and identity
snapshot. The existing identity database dependency invalidates records when
an identity is changed. This policy never approves candidates, replaces vision,
or grants transition readiness. Required current vision and approval evidence
remain mandatory.

## Verification boundaries

Use synthetic or explicitly shareable fixtures when calibrating profile bounds.
A passing geometry check alone is not full deterministic or workflow readiness:
palette adherence, vision, and explicit review remain separate requirements.
Do not publish private project measurements or provider character identifiers.

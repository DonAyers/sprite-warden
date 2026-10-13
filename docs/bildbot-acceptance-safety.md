# BildBot acceptance safety report — no live approval recorded

## Verified reproduction

Live `idle_pal` was read only. Its eight image records contain a passing
`direction_set` summary (10 pixel checks). Four completed `vision_reference`
failures remain: east, west, north-west, south-east. Three non-anchor comparisons
pass and the anchor is exempt from comparing with itself.

Isolated pilot:
`C:/Users/Don/AppData/Local/hermes/cache/scratch/accept-pilot-781f649fedb8478598509fc5602f91a4/report.json`

The pilot copied the actual images/measurements, rebound dependency paths to the
copy, backed up the identity DB using a read-only SQLite connection, and pointed
only its copied pipeline `views_dir` at `idle_pal`. That pointer change was a
fixture operation, not a live selection/approval. No override file was copied.
The hook's managed-character resolver selected `idle_pal` on the live read-only
probe despite the historical `views_dir` pointer.

Baseline: review false (4 of 8 blocked); PixelLab `animate_character` hook denied.
With actor `TEST isolated pilot (NOT Don approval)`: review true, hook allow;
original measurements preserved; every live source image/sidecar hash unchanged.
Only `check_call` was evaluated: no animation was sent, no provider/network spend.
The current classifier labels this call `animate_template` even when
`mode=pixminimax`; this report does not assert provider execution correctness.

## Proposed owner command — requires Don's explicit visual review first

Do not run this on Don's behalf merely because the implementation brief was
forwarded. Don should inspect the full set and the preserved findings in the
review tooling. If he explicitly agrees with the following rationale, he can run:

```console
warden accept "C:/Users/Don/Documents/Projects/i-require-maintenance/art_source/bots/bildbot/character-2026-10-11/idle_pal" --by Don --gate vision_reference --reason "I reviewed all eight current bildBot views and the vision findings. I accept the east/west emitter-side appearance and the north-west/south-east head surface details as this character's intended appearance. This approval applies only to the completed vision_reference artistic findings, not pixel checks or missing evidence."
warden review-check "C:/Users/Don/Documents/Projects/i-require-maintenance/art_source/bots/bildbot/character-2026-10-11/idle_pal" --require views
```

This is a **proposed** exact reason, not a quote from Don or evidence he approved
these specific findings. If he disagrees with any view, reject that result and
retain his feedback for the next iteration instead of accepting the directory.

## Policy boundaries / remaining integration

* The CLI cannot authenticate Don. Gallery review should provide the human
  interaction and call the reusable acceptance backend only after explicit action.
* Revision decisions stay authoritative; acceptance does not auto-migrate legacy
  narratives or promote revisions. Existing non-vision revision exception scopes
  remain an older-policy tension and are unavailable through `warden accept`.
* No game files, live sidecars, provider calls, gallery files or git commits were
  changed by this task. The existing identity prompt/tool-context improvement was
  not bundled into this acceptance change.

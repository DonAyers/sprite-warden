# Local pixel repair

Open an asset in the gallery and choose **Repair pixels locally**, or visit `/repair`.

1. Inspect original and candidate against the default neutral background. Choose checkerboard, muted green, or upload a game background. Backgrounds are preview-only.
2. Drag a rectangle on the original. Choose an exact source color and replacement from the sprite palette (click a swatch for source, Shift-click for replacement). Apply only inside the selection.
3. Undo/reset as needed. Alternatively upload your corrected PNG with identical dimensions: the tool derives exact changed pixels, including shading.
4. Inspect the changed-pixel preview and count. Download the source-bound JSON patch if useful.
5. Enter your name and repair reason, then **Save candidate**. This does not overwrite the original or accept gate findings.

Candidates are saved below the source folder in `.repairs/UUID/`, together with `patch.json` and `repair.json`. The catalog indexes the candidate. The original image, alpha (for masked RGB replacements), gate record, and approvals are untouched. Example uploads may alter alpha; the validation receipt reports whether it remained unchanged.

Saving runs local pixel-grid validation plus dimension/alpha preservation checks. These are diagnostic checks, not full stage validation. Direction-set checks, palette adherence, source-relative checks and artistic evidence must be rerun for the repaired candidate before promotion. No automatic vision/model calls occur.

The HTTP API uses catalog asset IDs, project containment, existing same-origin/CSRF protections and a 32 KiB request limit. Stale source digests are refused. Patches are tied to the exact source pixels and dimensions and must not be blindly applied to another facing. The current editor supports rectangular masks and exact color replacement, not freehand painting or automatic semantic defect localization.

Large repairs exceeding the request limit are rejected rather than silently truncated. New candidates remain pending human approval. Existing evidence is displayed for inspection but not inherited.

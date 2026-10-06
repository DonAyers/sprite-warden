# AGENTS.md

## Project
- `sprite-warden` — Python `src/`-layout package (import name `spritewarden`): pixel-art QA gates, SQLite asset catalog, generator routing, Aseprite integration, and an MCP server.
- CLI entrypoint `spritewarden.cli:main`; console scripts `warden`, `spritewarden`, `sprite-warden`.
- Managed with `uv` (hatchling build). Deps: pillow, pydantic, httpx, numpy, rich, mcp.

## Commands
- Install/sync: `uv sync` (a `.venv` already exists).
- Run all tests: `.venv\Scripts\python.exe -m pytest`
- Run one test: `.venv\Scripts\python.exe -m pytest tests/test_judge.py::test_pixel_grid_flat_vs_mixel`
- `uv run pytest` fails on this Windows machine with `uv trampoline failed to canonicalize script path` — invoke the venv python directly instead.
- CLI smoke tests: `warden judge-still <png> --palette luap-40 [--vision]`, `warden judge-anim <sheet.png> --cell 64 [--walk]`, `warden route "<desc>" --type vehicle`, `warden generate "<desc>" --type icon --provider spritecook --out <dir>`, `warden pad <files|dir> --size 64 [--align bottom-center]`, `warden sheet <files|dir> --out <sheet.png> --palette luap-40 [--engine auto|rust|python]`, `warden pixelize <img> --palette <lospec-slug> [--refine --vision]`, `warden learn seed && warden learn check && warden learn doctor`, `warden learn reflect`, `warden mcp`.

## Environment / API keys
- All LLM paths fall back to deterministic local heuristics when unauthenticated, so tests and the QA gates need no keys.
- `OPENROUTER_API_KEY` — Jev routing, Fusion deliberation, vision judging, OpenRouter image gen. On Windows, `_get_openrouter_key()` (router/jev.py) also reads it from the registry `HKCU\Environment` when the env var is empty.
- `SPRITE_FUSION_API_KEY`, `PIXELLAB_API_KEY` — provider drivers. `ASEPRITE` — path override for Aseprite.exe (else Windows install dirs are auto-discovered).
- `SPRITE_COOK_API_KEY` — SpriteCook driver (alias `SPRITECOOK_API_KEY`; base URL override `SPRITE_COOK_API_URL`/`SPRITECOOK_API_URL`). Read from env, with a Windows `HKCU\Environment` registry fallback like the OpenRouter key.
- `SPRITEWARDEN_PIXEL_SNAPPER` — path override for the Rust `spritefusion-pixel-snapper` binary used by `warden sheet` (else PATH, then `~/.cargo/bin`).

## Architecture
- `src/spritewarden/judge/` — deterministic QA gates (pixel_grid, palette_adherence, clipping, symmetry, animation_anomalies, walk_mechanics, loop_smoothness) plus `VisionJudge`/`OrchestratedVerdict` (vision + Jev). Entry fns: `evaluate_still`, `evaluate_animation`.
- `src/spritewarden/repo/` — `AssetCatalog` (SQLite + FTS5), `CharacterIdentity`/`SceneIdentity` models, palette registry. DB auto-created at `<repo>/data/spritewarden.db`.
- `src/spritewarden/reducer/` — image reducer (`pixelize`: downscale → k-means pre-quantize → dither → vectorized Oklab snap) + `refine` AI tuning loop (contact-sheet vision scoring, critique→param mapping, convergence). Palettes: builtin names, lospec slugs (`na16` / `lospec:<slug>`, cached in `~/.spritewarden/palettes`), JSON/.tres/.hex files.
- `src/spritewarden/learning/` — self-learning KB (`warden learn`): `db.py` (append-only `learning.db`), `models.py`, `taxonomy.py` (canonical perspective/style normalization), `registry.py` (seeded provider doc/tool sources), `acquisition.py` (conditional-GET fetch + snapshot; MCP `tools/list` probe), `mcp_probe.py` (minimal MCP stdio client), `diffing.py` (deterministic diff + significance), `episodes.py` (record attempts), `curation.py` (cluster → strategy + proposal; doc-event → proposal), `summarize.py` (optional capped LLM, deterministic fallback), `proposals.py` (review/apply gate), `scheduler.py` (background watcher), `doctor.py`, `tipshare_bridge.py`, `project_watch.py`, `cli.py`. Owns sources, snapshots, doc_events, provider_tools, episodes, strategies, proposals, tip_exports, budget. `PipelineRouter` appends `[learned]` rules from **active** strategies for the matching scope.
- `src/spritewarden/router/` — `PipelineRouter.route_request`: catalog dedup → Jev (`typesafe/jev-1.13`) or OpenRouter Fusion. `playbook.py` holds provider recipes + prompt sanitization; a `GenerationRecipe` carries provider-specific args via `recipe.params`.
- `src/spritewarden/generator/` — `BaseGeneratorDriver` + PixelLab / Sprite Fusion / OpenRouter / **SpriteCook** drivers, plus `registry.py` (`DRIVER_CLASSES`, `get_driver`, `dispatch`) so recipes are executable via `warden generate`. SpriteCook uses sync `/v1/api/generate-sync` + `/v1/api/animate-sync` (async job fallback), enforces the recipe palette via the API `colors` hex list, then re-snaps Oklab locally; asset IDs are canonical for reference/edit/animation.
- `src/spritewarden/sheet/` — `warden sheet`: `snap.py` (Rust `spritefusion-pixel-snapper` wrapper + pure-Python grid-detect/majority-resample + Oklab palette snap fallback; `find_pixel_snapper`, `snap_image`) and `builder.py` (`discover_inputs`, `build_sheet`/`build_sheets`: alpha-crop → uniform cell → align → grid compose + JSON manifest). `pad.py` — `pad_files`/`pad_image`: place provider output (varying canvas sizes, edge-hugging art) on a uniform aligned canvas (`--size`/`--canvas WxH`, per-frame `--grow N`, align/margin, `--fit clip|shrink`, optional palette snap, `--in-place`). MCP tools: `build_sprite_sheet`, `pad_sprite`.
- `src/spritewarden/server/` — MCP server (`warden mcp`); starts the learning scheduler daemon and exposes learning resources (`spritewarden://playbook|proposals|providers|learn-status`) + tools (`learn_status`, `list_learned_proposals`, `approve_learned_proposal`, `record_learning_episode`). `src/spritewarden/tools/` — Aseprite integration (Windows-only).
- `scripts/ingest_game_assets.py` — one-off seeder for the `i-require-maintenance` game.

## Gotchas
- Repo was renamed from `slaygent`. The `.venv` retains stale editable installs (`_editable_impl_slaygent.pth` / `_editable_impl_spritewarden.pth` → `../slaygent/src`) and stale `__pycache__`; tracebacks showing `slaygent\...` paths come from these remnants, not the current source.
- Three tests currently fail against the current code (pre-existing, not from new edits):
  - `test_router.py::test_pipeline_router_deduplication` — calls `route_request(character_id=...)`; the param is now `character_identity`.
  - `test_judge.py::test_pixel_grid_flat_vs_mixel` — mixel origin-detection heuristics.
  - `test_aseprite.py::test_write_gpl_palette` — expects `"Luap-40"`; the GPL writes lowercase `luap-40`.
- Built-in palettes are hardcoded in `judge/palette.py`; `luap-40` is the default reference palette (also `luap-40-plus`, `sweetie-16`).
- `load_palette` now treats any bare lowercase slug string (no path separators/extension, not a builtin) as a lospec palette-list slug and fetches it; unknown slugs raise FileNotFoundError only after the fetch fails. Override the cache with `SPRITEWARDEN_PALETTE_CACHE`.
- `test_find_aseprite` requires Aseprite to be installed on this host.
- Learning DB lives at `<repo>/data/learning.db`; override with `SPRITEWARDEN_LEARNING_DB`. Background-check cadence/caps: `SPRITEWARDEN_LEARN_INTERVAL`, `SPRITEWARDEN_LEARN_MONTHLY_USD`. The first check of a source only establishes a **baseline snapshot** (no event); `doc_events` fire only on real transitions.
- MCP `tools/list` drift probing needs a probe command per provider, e.g. `SPRITEWARDEN_MCP_PROBES='{"pixellab":["python","-m","pixellab_mcp"]}'`; without it `mcp_tools` sources are skipped silently.
- Lospec has no stable machine-readable index (its palette-list HTML is per-request dynamic), so it is **not** a watched source; palettes are fetched directly via `load_palette`/`pixelize --palette`.
- `warden sheet` resolves the Rust snapper via `SPRITEWARDEN_PIXEL_SNAPPER` → PATH → `~/.cargo/bin` (install: `cargo install spritefusion-pixel-snapper`). Rust palette recolor is straight RGB, so the builder re-snaps with Oklab afterwards; when the binary is missing/errors it silently uses the Python grid-detect + majority-resample fallback.

## Outstanding / TODO
- `warden sheet`: add optional `--register --game <slug>` to register composed frames into the `AssetCatalog` (currently pure file output with a JSON manifest; no catalog asset IDs are minted).
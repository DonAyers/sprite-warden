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
- CLI smoke tests: `warden judge-still <png> --palette luap-40 [--vision]`, `warden judge-anim <sheet.png> --cell 64 [--walk]`, `warden route "<desc>" --type vehicle`, `warden pixelize <img> --palette <lospec-slug> [--refine --vision]`, `warden learn seed && warden learn check`, `warden mcp`.

## Environment / API keys
- All LLM paths fall back to deterministic local heuristics when unauthenticated, so tests and the QA gates need no keys.
- `OPENROUTER_API_KEY` — Jev routing, Fusion deliberation, vision judging, OpenRouter image gen. On Windows, `_get_openrouter_key()` (router/jev.py) also reads it from the registry `HKCU\Environment` when the env var is empty.
- `SPRITE_FUSION_API_KEY`, `PIXELLAB_API_KEY` — provider drivers. `ASEPRITE` — path override for Aseprite.exe (else Windows install dirs are auto-discovered).

## Architecture
- `src/spritewarden/judge/` — deterministic QA gates (pixel_grid, palette_adherence, clipping, symmetry, animation_anomalies, walk_mechanics, loop_smoothness) plus `VisionJudge`/`OrchestratedVerdict` (vision + Jev). Entry fns: `evaluate_still`, `evaluate_animation`.
- `src/spritewarden/repo/` — `AssetCatalog` (SQLite + FTS5), `CharacterIdentity`/`SceneIdentity` models, palette registry. DB auto-created at `<repo>/data/spritewarden.db`.
- `src/spritewarden/reducer/` — image reducer (`pixelize`: downscale → k-means pre-quantize → dither → vectorized Oklab snap) + `refine` AI tuning loop (contact-sheet vision scoring, critique→param mapping, convergence). Palettes: builtin names, lospec slugs (`na16` / `lospec:<slug>`, cached in `~/.spritewarden/palettes`), JSON/.tres/.hex files.
- `src/spritewarden/learning/` — self-learning KB (`warden learn`): `db.py` (append-only `learning.db`), `models.py`, `taxonomy.py` (canonical perspective/style normalization), `registry.py` (seeded provider doc/tool sources), `acquisition.py` (conditional-GET fetch + snapshot), `diffing.py` (deterministic diff + significance), `cli.py`. Owns sources, snapshots, doc_events, provider_tools, episodes, strategies, proposals, budget.
- `src/spritewarden/router/` — `PipelineRouter.route_request`: catalog dedup → Jev (`typesafe/jev-1.13`) or OpenRouter Fusion. `playbook.py` holds provider recipes + prompt sanitization; a `GenerationRecipe` carries provider-specific args via `recipe.params`.
- `src/spritewarden/generator/` — `BaseGeneratorDriver` + PixelLab / Sprite Fusion / OpenRouter drivers.
- `src/spritewarden/server/` — MCP server (`warden mcp`). `src/spritewarden/tools/` — Aseprite integration (Windows-only).
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
- Learning DB lives at `<repo>/data/learning.db`; override with `SPRITEWARDEN_LEARNING_DB`. Background-check cadence/caps: `SPRITEWARDEN_LEARN_INTERVAL`, `SPRITEWARDEN_LEARN_MONTHLY_USD`. The `mcp_tools` source kind is a placeholder until the L3 scheduler wires real MCP `tools/list` probing; `check` skips it.
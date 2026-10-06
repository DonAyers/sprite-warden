# sprite-warden

**The Intelligent Pixel Art Pipeline, Repository, Quality Warden & Smart Router**

`sprite-warden` (`warden`, `spritewarden`) is a developer & agent-first tooling suite for pixel art games. It coordinates:

1. **Deterministic Quality Gates**:
   - `pixel_grid`: Detects mixels, anti-aliased edge blurring, and fractional scaling.
   - `palette_adherence`: Evaluates exact Oklab color distance and suggests palette mappings.
   - `clipping`: Detects sliced edges or out-of-bounds sprites.
   - `symmetry`: Evaluates silhouette symmetry for front and back start poses.
   - `animation_anomalies`: Finds 1-frame transient specks and ground contact spikes.
   - `walk_mechanics`: Verifies alternating foot cadence (catches hops, skips, and jumping jacks).
   - `loop_smoothness`: Measures step pixel deltas, catches hesitation stalls and wrap jumps, and retimes loops.
2. **Visual Smell-Test & Decision Orchestration**:
   - Multimodal vision model integration (e.g. Gemini 2.5 Flash / Claude Opus) on 6x nearest-neighbor board.
   - Orchestrated through **Jev Decision Model** (`typesafe/jev-1.13`) to return typed actions: `ACCEPT`, `PALETTE_REMAP`, `RETIME_STALLS`, `INPAINT_PART`, `REROLL`.
3. **Central Repository & Character Identity**:
   - Maintains consistent Character Identities with assigned palettes, ground offsets, prompts, and negative constraints (`[no toes]`, `[no human limbs]`).
   - SQLite FTS5 search index to deduplicate assets before generating.
4. **Smart Generator Routing & Deliberation**:
   - Fast-path routing with Jev to proven provider workflows (PixelLab, Sprite Fusion, Retro Diffusion, OpenRouter Nano Banana 2, SpriteCook).
   - Multi-model deliberation with **OpenRouter Fusion** (`openrouter/fusion`) for novel/untested asset types.
   - Executable provider drivers (`spritewarden generate`) for PixelLab, Sprite Fusion, OpenRouter Image, and SpriteCook.
5. **Sprite Sheet Builder & Padding**:
   - `spritewarden pad <files|folder> --size 64`: normalize provider output (varying canvases, art hugging the edges) onto a uniform, ground-aligned canvas — fixed `--size`/`--canvas WxH`, per-frame `--grow N`, align, margin, power-of-two, oversized `--fit clip|shrink`, optional palette snap, batch or `--in-place`.
   - `spritewarden sheet <files|folder> --out sheet.png`: discovers and orders frames (natural sort), snaps them to their implicit pixel grid, palette-locks them, and composes an evenly-celled, ground-aligned spritesheet + JSON manifest.
   - Grid snapping uses the Rust `spritefusion-pixel-snapper` when present, with a pure-Python fallback (grid detection + majority-vote resample + Oklab palette snap) so it never hard-fails.
6. **Aseprite Integration**:
   - `spritewarden open <path>`: Launch Aseprite immediately for inspection.
   - `spritewarden edit <sheet.png> --cell 64 --palette luap-40`: Interactive editing with auto-export on save and live QA checking.
7. **Agent MCP Server**:
   - Tools: `search_art`, `get_character_identity`, `register_character_identity`, `route_generation`, `judge_still`, `judge_animation`, `pixelize_image`, `pad_sprite`, `build_sprite_sheet`, `open_in_aseprite`, `learn_status`, `list_learned_proposals`, `approve_learned_proposal`, `record_learning_episode`.
   - Resources: `spritewarden://playbook`, `spritewarden://proposals`, `spritewarden://providers`, `spritewarden://learn-status`.

## Quick CLI Usage

```bash
# Evaluate a still sprite with vision inspection
spritewarden judge-still assets/sprites/bot.png --palette luap-40 --vision

# Evaluate an animation spritesheet
spritewarden judge-anim assets/sprites/walk.png --cell 64 --walk

# Open any asset in Aseprite
spritewarden open assets/sprites/smolbot.png

# Interactive Aseprite editing with auto-export on Ctrl+S
spritewarden edit assets/sprites/smolbot_topdown.png --cell 64

# Route a generation request
spritewarden route "dual tread robot chassis" --vehicle

# Route and execute through the provider driver (e.g. SpriteCook)
spritewarden generate "health potion icon" --type icon --provider spritecook --out assets/icons

# Combine loose frames/folder into a palette-snapped, evenly-framed spritesheet
spritewarden sheet assets/sprites/raw --out assets/sprites/walk.png --palette luap-40 --columns 8

# Normalize provider output (varying sizes, edge-hugging art) to uniform 64x64 cells first
spritewarden pad assets/sprites/raw --size 64 --align bottom-center

# Start the MCP server for AI agents
spritewarden mcp
```

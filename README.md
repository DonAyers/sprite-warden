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

# Check one generator tool call against the recipe registry (exit 2 = deny)
warden check-call --tool mcp__plugin_spritecook_spritecook__animate_game_art --input-file call.json --json

# Judge an animation against the still it was made from (catches a keyed-out fill)
warden judge-anim walk.png --cell 64 --walk --char-id bildbot --source anchor.png

# Compare every view of a direction set with the anchor by vision model (paid, opt-in)
warden judge-views art/bot/views --anchor art/bot/anchor.png --vision

# Record what the judges should know about a character
warden identity set bildbot --desc "one-wheel builder bot" --locomotion wheels --body-class figure \
  --size-tier 96 --feature wrench:left:#ff00ff --still south=art/bot/south.png

# Register a composed sheet in the catalog; export the registry as tipshare tips (dry run)
warden sheet frames/ --out walk.png --register --game i-require-maintenance
warden recipes --export-tips tips.json
```

## Enforcement: the recipe registry as a gatekeeper

Each provider's documented rules live in `src/spritewarden/data/recipes.json`. Its `enforce` blocks
are executed by `spritewarden.enforce.check_call(tool_name, tool_input, project_root)`, which answers
`allow`, `deny` or `ignore` for a tool call an agent is about to make. It denies:

- a rotation or animation whose source image has no current passed gate record (`<image>.gate.json`),
  resolved from CLI `--input`, a SpriteCook asset id, a PixelLab reference URL or a PixelLab character id
  through the project's asset manifest;
- a rotation or animation by a provider other than the one the nearest `pipeline.json` records;
- arguments a provider documents as invalid (more than 8 SpriteCook `colors`, Sprite Fusion `edit --size`,
  a `direction-set` size outside 16/32/64, Retro Diffusion animation sizes that are not multiples of 8,
  inline base64 references to PixelLab, a looping motion on `pixel-engine-v1.5`, the prompt enhancer off);
- the provider's forbidden prompt words and pixel dimensions in a Sprite Fusion prompt;
- a SpriteCook animation whose background matte colour is in the sprite (it would be keyed out of every frame);
- publishing a review page whose candidate images are not gated.

Use it as a Claude Code hook (`.claude/settings.json`):

```json
{"hooks": {"PreToolUse": [{
  "matcher": "mcp__pixellab__.*|mcp__plugin_spritecook_spritecook__.*|Bash|PowerShell|Artifact",
  "hooks": [{"type": "command", "command": "warden hook --claude", "timeout": 10}]
}]}}
```

A call that is not a generator call costs about 80 ms. Optional project config, `spritewarden.json` at the
project root (the values shown are the defaults):

```json
{
  "manifest": "art_source/spritecook-assets.json",
  "override": "tmp/generator_hook_override.json",
  "cli_tools": {"tools/sprite_fusion.py": "spritefusion", "tools/retro_diffusion.py": "retrodiffusion"},
  "review_specs": true
}
```

The manifest is a JSON file whose list or dict values hold entries `{"asset_id" | "url", "file"}`, plus
`"pixellab_characters": [{"character_id", "dir"}]`. To override a deny on purpose, write
`{"reason": "...", "calls": 1}` to the override file: each overridden call decrements it and the rules it
skipped are reported. `warden generate` refuses the same un-gated inputs (`--force-ungated` to run anyway)
and registers its outputs, with provider asset ids and params, in the catalog.

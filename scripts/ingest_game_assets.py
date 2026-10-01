"""Ingest existing project assets from i-require-maintenance into spritewarden's catalog."""

from pathlib import Path
from spritewarden.repo.catalog import AssetCatalog
from spritewarden.repo.identity import CharacterIdentity, FeatureLandmark

SPRITES_DIR = Path(r"C:\Users\Don\Documents\Projects\i-require-maintenance\assets\sprites")


def main():
    cat = AssetCatalog()

    # 1. Register canonical bots
    smolbot = CharacterIdentity(
        id="smolbot",
        name="SmolBot",
        game="i-require-maintenance",
        palette_id="luap-40",
        perspective="low top-down",
        dimensions=[64, 64],
        ground_row_offset=7,
        prompt_description="small gray cuboid robot with green visor and antenna",
        prohibitions=["no toes", "no organic parts", "no human limbs"],
        features={
            "visor": FeatureLandmark(name="visor", description="emerald green visor", marker_hex="#6da18a"),
            "antenna": FeatureLandmark(name="antenna", description="single stalk on top right", marker_hex="#de9e41"),
        },
        turnaround_stills={"south": str(SPRITES_DIR / "smolbot_topdown.png")},
    )
    cat.save_identity(smolbot)

    tredbot = CharacterIdentity(
        id="tredbot",
        name="TredBot",
        game="i-require-maintenance",
        palette_id="luap-40",
        perspective="low top-down",
        dimensions=[96, 96],
        ground_row_offset=7,
        prompt_description="heavy industrial tread-base robot with turret and shield",
        prohibitions=["no legs"],
        turnaround_stills={"south": str(SPRITES_DIR / "tredbot.png")},
    )
    cat.save_identity(tredbot)

    # 2. Scan and index sprites
    count = 0
    if SPRITES_DIR.exists():
        for png in SPRITES_DIR.rglob("*.png"):
            if ".aseprite_work" in png.parts or "tmp" in png.parts:
                continue
            name = png.stem.replace("_", " ").title()
            cat_type = "characters" if any(b in png.stem for b in ["bot", "slime", "knight", "beetle"]) else "props"
            char_id = "smolbot" if "smolbot" in png.stem else ("tredbot" if "tredbot" in png.stem else None)
            
            cat.register_asset(
                asset_id=f"irm_{png.stem}",
                name=name,
                file_path=png,
                category=cat_type,
                asset_type="spritesheet" if "sheet" in png.stem or "walk" in png.stem or "topdown" in png.stem else "sprite",
                game="i-require-maintenance",
                character_id=char_id,
                palette_id="luap-40",
                perspective="low top-down",
                tags=[png.stem, cat_type] + ([char_id] if char_id else []),
            )
            count += 1

    print(f"Ingested {count} assets and 2 character identities into spritewarden catalog.")


if __name__ == "__main__":
    main()

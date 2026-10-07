"""Aseprite integration: open sprites for inspection, and bidirectional sheet editing."""

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from PIL import Image

from ..judge.palette import load_palette, palette_display_name, rgb_to_hex
from ..judge import evaluate_still

CANDIDATES = [
    r"C:\Program Files\Aseprite\Aseprite.exe",
    r"C:\Program Files (x86)\Aseprite\Aseprite.exe",
    r"C:\Program Files (x86)\Steam\steamapps\common\Aseprite\Aseprite.exe",
    r"C:\Program Files\Steam\steamapps\common\Aseprite\Aseprite.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Aseprite\Aseprite.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Aseprite\Aseprite.exe"),
]

IMPORT_LUA = """
local p = app.params
local sprite = app.open(p.png)
if not sprite then
  error("couldn't open " .. tostring(p.png))
end
local cell = tonumber(p.cell)

app.command.ImportSpriteSheet {
  ui = false,
  type = SpriteSheetType.ROWS,
  frameBounds = Rectangle(0, 0, cell, cell),
}

if p.gpl and p.gpl ~= "" then
  sprite:setPalette(Palette { fromFile = p.gpl })
end
sprite:saveAs(p.out)
"""


def find_aseprite() -> Path | None:
    """Locate Aseprite.exe on the system."""
    override = os.environ.get("ASEPRITE", "")
    if override and Path(override).is_file():
        return Path(override)
    for c in CANDIDATES:
        p = Path(c)
        if p.is_file():
            return p
    lib_file = Path(r"C:\Program Files (x86)\Steam\steamapps\libraryfolders.vdf")
    if lib_file.is_file():
        try:
            with open(lib_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"path"' in line:
                        lib = line.split('"')[3].replace("\\\\", "\\")
                        p = Path(lib) / "steamapps" / "common" / "Aseprite" / "Aseprite.exe"
                        if p.is_file():
                            return p
        except Exception:
            pass
    return None


def get_aseprite_exe() -> Path:
    exe = find_aseprite()
    if not exe:
        raise FileNotFoundError(
            "Aseprite executable not found. Please ensure Aseprite is installed or set the ASEPRITE environment variable."
        )
    return exe


def run_batch(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Execute Aseprite in headless batch mode."""
    exe = get_aseprite_exe()
    res = subprocess.run([str(exe), "-b"] + args, capture_output=True, text=True, errors="replace")
    if res.returncode != 0:
        raise RuntimeError(f"Aseprite batch command failed (exit {res.returncode}): {res.stderr}")
    return res


def write_gpl_palette(palette_id: str, output_path: str | Path) -> Path:
    """Generate a GIMP palette (.gpl) from a registered spritewarden palette."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    colors = load_palette(palette_id)
    lines = [
        "GIMP Palette",
        f"Name: {palette_display_name(palette_id)}",
        "Columns: 8",
        "#",
    ]
    for i, (r, g, b) in enumerate(colors):
        lines.append(f"{r:3d} {g:3d} {b:3d}\t{palette_id}_{i}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def open_file(file_path: str | Path) -> subprocess.Popen:
    """Simply launch Aseprite on a file for direct user inspection."""
    exe = get_aseprite_exe()
    target = Path(file_path).resolve()
    if not target.is_file():
        raise FileNotFoundError(f"File not found: {file_path}")
    return subprocess.Popen([str(exe), str(target)])


def import_sheet_to_aseprite(
    sheet_path: str | Path,
    cell: int,
    palette_id: str = "luap-40",
    work_dir: str | Path | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    """Convert a PNG sheet into an .aseprite file with frame cells and palette swatches."""
    sheet = Path(sheet_path).resolve()
    if not sheet.is_file():
        raise FileNotFoundError(f"Sheet file not found: {sheet_path}")

    img = Image.open(sheet)
    if img.width % cell or img.height % cell:
        raise ValueError(f"Sheet size {img.width}x{img.height} is not divisible by cell size {cell}px")

    w_dir = Path(work_dir) if work_dir else sheet.parent / ".aseprite_work"
    w_dir.mkdir(parents=True, exist_ok=True)

    gpl_path = w_dir / f"{palette_id}.gpl"
    write_gpl_palette(palette_id, gpl_path)

    lua_script = w_dir / "import_sheet.lua"
    lua_script.write_text(IMPORT_LUA, encoding="utf-8")

    ase_path = w_dir / f"{sheet.stem}.aseprite"
    record = {
        "sheet": str(sheet),
        "cell": cell,
        "columns": img.width // cell,
        "rows": img.height // cell,
        "size": [img.width, img.height],
        "palette_id": palette_id,
    }
    record_path = w_dir / f"{sheet.stem}_record.json"
    record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")

    run_batch([
        "--script-param", f"png={sheet}",
        "--script-param", f"cell={cell}",
        "--script-param", f"gpl={gpl_path}",
        "--script-param", f"out={ase_path}",
        "--script", str(lua_script),
    ])
    return ase_path, record_path, record


def export_aseprite_to_sheet(ase_path: str | Path, record: dict[str, Any]) -> tuple[bool, str]:
    """Export an .aseprite file back over its source PNG sheet, with automated QA verification."""
    sheet_target = Path(record["sheet"])
    tmp_export = sheet_target.parent / f".tmp_{sheet_target.name}"

    run_batch([
        str(ase_path),
        "--sheet", str(tmp_export),
        "--data", str(tmp_export.with_suffix(".json")),
        "--sheet-type", "rows",
        "--sheet-columns", str(record["columns"]),
    ])

    exported_img = Image.open(tmp_export)
    if exported_img.size != tuple(record["size"]):
        return False, f"Export size {exported_img.size} does not match expected {record['size']}"

    # Evaluate palette adherence before overwriting
    card = evaluate_still(exported_img, palette_source=record.get("palette_id", "luap-40"))
    exported_img.save(sheet_target)
    tmp_export.unlink(missing_ok=True)
    tmp_export.with_suffix(".json").unlink(missing_ok=True)

    pal_gate = card.gate("palette_adherence")
    if pal_gate and not pal_gate.passed:
        return True, f"Exported {sheet_target.name} with warnings: {pal_gate.summary}"
    return True, f"Exported {sheet_target.name} (100% on palette)"


def edit_sheet_interactive(
    sheet_path: str | Path,
    cell: int = 64,
    palette_id: str = "luap-40",
    timeout: int = 1800,
    callback: Any = None,
) -> None:
    """Open a sheet in Aseprite and watch for saves, exporting back to the PNG and checking QA."""
    ase_path, record_path, record = import_sheet_to_aseprite(sheet_path, cell, palette_id)
    started = time.time()
    proc = subprocess.Popen([str(get_aseprite_exe()), str(ase_path)])

    if callback:
        callback("opened", f"Opened {ase_path.name} in Aseprite. Watching for saves...")

    seen = ase_path.stat().st_mtime
    handed_off = False

    while True:
        time.sleep(1.0)
        if proc.poll() is not None and not handed_off:
            if time.time() - started < 5.0:
                handed_off = True
                if callback:
                    callback("info", f"Aseprite was already running; watching for saves for {timeout // 60}m")
            else:
                if callback:
                    callback("closed", "Aseprite closed.")
                break

        if handed_off and time.time() - started > timeout:
            if callback:
                callback("timeout", "Stopped watching (--timeout reached).")
            break

        try:
            mod = ase_path.stat().st_mtime
        except OSError:
            continue

        if mod != seen:
            time.sleep(0.5)
            seen = ase_path.stat().st_mtime
            ok, msg = export_aseprite_to_sheet(ase_path, record)
            if callback:
                callback("saved" if ok else "error", msg)

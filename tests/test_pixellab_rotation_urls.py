"""Offline registered PixelLab rotation URL resolution and hook evidence."""
import json
import subprocess
import sys

import pytest
from PIL import Image

from spritewarden.enforce import check_call, load_config
from spritewarden.enforce.core import _Manifest
from spritewarden.gates.records import write_record, mark_rejected

CHAR = "1dac29a3-6454-4296-8e6d-49df6587ceba"
BASE = f"https://backblaze.pixellab.ai/file/pixellab-characters/account/{CHAR}/rotations/"


def project(root, facing="north", shape="list"):
    folder = root / "art_source/bots/bildbot/character-2026-10-11/idle"
    folder.mkdir(parents=True)
    image = folder / f"{facing}.png"
    Image.new("RGBA", (32, 32), (200, 60, 60, 255)).save(image)
    chars = [{"character_id": CHAR, "dir": folder.relative_to(root).as_posix()}]
    if shape == "dict":
        chars = {CHAR: {"dir": folder.relative_to(root).as_posix()}}
    manifest = root / "art_source/spritecook-assets.json"
    manifest.write_text(json.dumps({"pixellab_characters": chars}))
    return image, _Manifest(load_config(root))


@pytest.mark.parametrize("query", ["", "?download=1&sig=abc"])
@pytest.mark.parametrize("shape", ["list", "dict"])
def test_registered_rotation_resolves(tmp_path, query, shape):
    image, manifest = project(tmp_path, shape=shape)
    assert manifest.url_file(BASE + "north.png" + query) == image.resolve()


@pytest.mark.parametrize("url_facing,local_facing", [
    ("south-west", "south-west"), ("south_west", "south-west"),
    ("southwest", "south_west"), ("sw", "south-west"),
])
def test_explicit_facing_aliases(tmp_path, url_facing, local_facing):
    image, manifest = project(tmp_path, local_facing)
    assert manifest.url_file(BASE + url_facing + ".png") == image.resolve()


@pytest.mark.parametrize("url", [
    BASE.replace("https:", "http:") + "north.png",
    BASE.replace("backblaze.pixellab.ai", "backblaze.pixellab.ai.evil.test") + "north.png",
    BASE.replace("backblaze.pixellab.ai", "evil@backblaze.pixellab.ai") + "north.png",
    BASE.replace("backblaze.pixellab.ai", "backblaze.pixellab.ai:443") + "north.png",
    BASE.replace(CHAR, "unknown") + "north.png",
    BASE + "unknown.png", BASE + "../north.png", BASE + "%2e%2e/north.png",
    BASE + "north%2f.png", BASE + "north%5c.png", BASE + "north.png/extra",
    BASE + "north.png#fragment", BASE + "north.PNG",
    BASE.replace("/account/", "/../" ) + "north.png",
    BASE.replace("/account/", "/%2f/" ) + "north.png",
    BASE + "north\\.png", " " + BASE + "north.png",
])
def test_invalid_rotation_urls_do_not_resolve(tmp_path, url):
    _, manifest = project(tmp_path)
    assert manifest.url_file(url) is None


def test_exact_registration_still_takes_precedence(tmp_path):
    image, _ = project(tmp_path)
    other = tmp_path / "explicit.png"
    other.write_bytes(b"explicit registration")
    path = tmp_path / "art_source/spritecook-assets.json"
    data = json.loads(path.read_text())
    url = BASE + "north.png?sig=1"
    data["hosted"] = [{"url": url, "file": "explicit.png"},
                      {"url": "https://generic.example/image", "file": "explicit.png"}]
    path.write_text(json.dumps(data))
    manifest = _Manifest(load_config(tmp_path))
    assert manifest.url_file(url) == other
    assert manifest.url_file(BASE + "north.png?sig=2") == image.resolve()
    assert manifest.url_file("https://generic.example/image") == other
    assert manifest.url_file("https://generic.example/image?changed=1") is None


def test_exact_facing_is_not_replaced_by_gated_alias(tmp_path):
    image, manifest = project(tmp_path, "south-west")
    alias = image.with_name("sw.png")
    alias.write_bytes(b"different alias bytes")
    write_record(alias, {"views": True})
    assert manifest.url_file(BASE + "south-west.png") == image.resolve()


def test_ambiguous_fallback_cannot_borrow_gated_alias(tmp_path):
    image, manifest = project(tmp_path, "south_west")
    alias = image.with_name("sw.png")
    alias.write_bytes(b"different gated alias")
    write_record(alias, {"views": True})
    assert manifest.url_file(BASE + "south-west.png") is None
    verdict = check_call("mcp__pixellab__animate_character", {
        "character_id": CHAR, "mode": "pixminimax", "frame_count": 16,
        "directions": ["south-west"], "end_frame_url": BASE + "south-west.png"}, tmp_path)
    assert verdict.decision == "deny", verdict.reasons


def test_view_link_outside_registered_folder_does_not_resolve(tmp_path):
    image, manifest = project(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside registered folder")
    image.unlink()
    try:
        image.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")
    assert manifest.url_file(BASE + "north.png") is None


def test_missing_local_view_does_not_resolve(tmp_path):
    image, manifest = project(tmp_path)
    image.unlink()
    assert manifest.url_file(BASE + "north.png") is None


def test_registered_folder_outside_project_does_not_resolve(tmp_path):
    image, _ = project(tmp_path)
    path = tmp_path / "art_source/spritecook-assets.json"
    path.write_text(json.dumps({"pixellab_characters": {CHAR: {"dir": str(tmp_path.parent)}}}))
    (tmp_path.parent / "north.png").write_bytes(b"outside")
    try:
        assert _Manifest(load_config(tmp_path)).url_file(BASE + "north.png") is None
    finally:
        (tmp_path.parent / "north.png").unlink()


@pytest.mark.parametrize("state,reason", [
    ("ready", None), ("ungated", "no gate record"),
    ("rejected", "rejected"), ("stale", "changed since"),
    ("incomplete", "no current passed gate"),
])
@pytest.mark.parametrize("query", ["", "?sig=1"])
def test_hook_end_frame_uses_current_local_evidence(tmp_path, state, reason, query):
    image, _ = project(tmp_path)
    if state != "ungated":
        params = {"command": "judge-views", "anchor": "south.png"} if state == "incomplete" else None
        write_record(image, {"views": True}, params=params)
    if state == "rejected":
        mark_rejected(image, by="owner", reason="wrong design")
    if state == "stale":
        image.write_bytes(b"changed evidence")
    args = {"character_id": CHAR, "mode": "pixminimax", "frame_count": 16,
            "directions": ["north"], "end_frame_url": BASE + "north.png" + query}
    verdict = check_call("mcp__pixellab__animate_character", args, tmp_path)
    assert verdict.decision == ("allow" if reason is None else "deny"), verdict.reasons
    assert not any("does not resolve" in r for r in verdict.reasons)
    if reason:
        assert any(reason in r for r in verdict.reasons), verdict.reasons
    event = {"tool_name": "mcp__pixellab__animate_character", "tool_input": args}
    out = subprocess.run([sys.executable, "-m", "spritewarden.cli", "hook", "--claude", "--project", str(tmp_path)],
                         input=json.dumps(event), text=True, capture_output=True)
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)["hookSpecificOutput"]
    assert result.get("permissionDecision") == (None if reason is None else "deny")

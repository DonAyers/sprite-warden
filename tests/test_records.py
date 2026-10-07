"""Tests for gate records (<image>.gate.json) and the review gatekeeper."""

import json
import subprocess
import sys
from pathlib import Path

from PIL import Image

from spritewarden.gates import (
    gates_from_scorecard,
    is_gated,
    load_record,
    passed_gates,
    record_path,
    require_passed,
    sha256_of,
    write_record,
)
from spritewarden.judge import evaluate_still

BODY = (89, 112, 128, 255)


def _anchor(path: Path, canvas: int = 64) -> Path:
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    for y in range(20, 60):
        for x in range(22, 42):
            img.putpixel((x, y), BODY)
    img.save(path)
    return path


def test_write_and_read_record(tmp_path: Path):
    img = _anchor(tmp_path / "south.png")
    card = evaluate_still(img, "luap-40", start_image=True, target_height=40)
    gates = gates_from_scorecard(card, "still")
    rec = write_record(img, gates, params={"command": "test"})
    assert rec == record_path(img) == tmp_path / "south.png.gate.json"

    data = json.loads(rec.read_text(encoding="utf-8"))
    # The shape the game repo's generator hook reads.
    assert data["sha256"] == sha256_of(img)
    assert data["gates"]["start_image"]["passed"] is True
    assert data["gates"]["still"] == {"passed": True}
    assert data["params"]["command"] == "test"
    assert "time" in data and data["spritewarden"]
    assert load_record(img) == data and load_record(rec) == data

    assert is_gated(img, "start_image") and is_gated(img, "pixel_grid") and is_gated(img, "still")
    assert not is_gated(img, "views")
    assert {"start_image", "still", "pixel_grid"} <= passed_gates(img)


def test_changed_image_is_no_longer_gated(tmp_path: Path):
    img = _anchor(tmp_path / "a.png")
    write_record(img, {"start_image": True, "still": {"passed": True}})
    assert is_gated(img, "start_image")
    pic = Image.open(img).convert("RGBA")
    pic.putpixel((5, 5), (255, 255, 255, 255))
    pic.save(img)
    assert is_gated(img, "start_image") is False
    assert passed_gates(img) == set()
    assert load_record(img) is not None  # the stale file still loads, it just no longer counts


def test_failed_verdicts_do_not_count(tmp_path: Path):
    img = _anchor(tmp_path / "b.png")
    write_record(img, {"start_image": {"passed": False, "summary": "strays"}, "pixel_grid": True})
    assert passed_gates(img) == {"pixel_grid"}
    assert is_gated(img, "start_image") is False
    assert is_gated(tmp_path / "missing.png", "still") is False


def test_require_passed_lists_missing_and_stale(tmp_path: Path):
    good = _anchor(tmp_path / "good.png")
    write_record(good, {"start_image": True, "still": True})
    ungated = _anchor(tmp_path / "ungated.png")
    stale = _anchor(tmp_path / "stale.png")
    write_record(stale, {"start_image": True})
    Image.new("RGBA", (64, 64), (1, 2, 3, 255)).save(stale)
    wrong_gate = _anchor(tmp_path / "wrong.png")
    write_record(wrong_gate, {"anim": True})

    report = require_passed([tmp_path], ["start_image"])
    assert report["ok"] is False and report["checked"] == 4
    reasons = {Path(m["path"]).name: m["reason"] for m in report["missing"]}
    assert "ungated.png" in reasons and "record" in reasons["ungated.png"]
    assert "sha256" in reasons["stale.png"]
    assert "start_image" in reasons["wrong.png"]
    assert report["passed"] == [str(good)]

    # any one of several names satisfies
    assert require_passed([wrong_gate], ["start_image", "anim"])["ok"] is True
    assert require_passed([good], ["still"])["ok"] is True
    assert require_passed([tmp_path / "nothing"], ["still"])["ok"] is False


def test_cli_judge_still_writes_record_and_review_check_reads_it(tmp_path: Path):
    img = _anchor(tmp_path / "cli.png")
    cmd = [sys.executable, "-m", "spritewarden.cli", "judge-still", str(img), "--palette", "luap-40",
           "--start-image", "--target-height", "40", "--json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert record_path(img).is_file()
    assert is_gated(img, "start_image") and is_gated(img, "still")
    data = load_record(img)
    assert data["params"]["command"] == "judge-still" and data["params"]["target_height"] == 40

    ok = subprocess.run([sys.executable, "-m", "spritewarden.cli", "review-check", str(img), "--require", "start_image"],
                        capture_output=True, text=True, timeout=120)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    other = _anchor(tmp_path / "other.png")
    bad = subprocess.run([sys.executable, "-m", "spritewarden.cli", "review-check", str(tmp_path), "--require", "views"],
                         capture_output=True, text=True, timeout=120)
    assert bad.returncode == 1
    assert "other.png" in bad.stdout and "cli.png" in bad.stdout

    # --no-record skips the sidecar
    other_rec = record_path(other)
    subprocess.run([sys.executable, "-m", "spritewarden.cli", "judge-still", str(other), "--no-record", "--json"],
                   capture_output=True, text=True, timeout=120)
    assert not other_rec.exists()

"""Offline regression coverage for the final provenance/CLI integration."""

import os
import subprocess
import sys

import pytest
from PIL import Image

from spritewarden.generator import registry as gen_registry
from spritewarden.generator.base import BaseGeneratorDriver, GenerationOutput
from spritewarden.gates.records import stage_readiness
from spritewarden.repo.catalog import AssetCatalog
from spritewarden.router.playbook import GenerationRecipe


@pytest.mark.parametrize("empty", [False, True], ids=["failed-gates", "validation-exception"])
def test_completed_failed_validation_remains_a_diagnostic_candidate(tmp_path, monkeypatch, empty):
    class Driver(BaseGeneratorDriver):
        def is_configured(self):
            return True

        def generate(self, recipe, output_dir):
            path = output_dir / "candidate.png"
            Image.new("RGBA", (16, 16), (0, 0, 0, 0) if empty else (1, 2, 3, 255)).save(path)
            return GenerationOutput(success=True, provider="spritecook", output_files=[str(path)],
                                    metadata={"asset_ids": ["native-diagnostic"], "credits_used": 3})

    monkeypatch.setitem(gen_registry.DRIVER_CLASSES, "spritecook", Driver)
    catalog = AssetCatalog(tmp_path / "catalog.db")
    recipe = GenerationRecipe(provider="spritecook", operation="generate", prompt="diagnostic gear", size=32,
                              canvas_size=32, perspective="top-down", params={"mode": "assets"})
    output = gen_registry.dispatch(recipe, tmp_path / "out", project_root=tmp_path, catalog=catalog)
    assert not output.success
    assert output.metadata["execution"] == "completed"
    assert output.metadata["validation"] == "failed"
    ids = output.metadata["catalog_asset_ids"]
    assert len(ids) == 1
    provenance = catalog.get_asset(ids[0])["provenance"]
    assert provenance["provider_asset_id"] == "native-diagnostic"
    assert provenance["credits_used"] == 3
    assert provenance["execution"] == "completed"
    assert provenance["validation"] == "failed"
    assert provenance["readiness"] == "blocked"
    assert provenance["human_decision"] == "pending"
    assert provenance["run_id"] == output.metadata["run_id"]
    assert provenance["gate_records"] == output.metadata.get("gate_records", [])
    assert provenance["error"] == output.error
    assert all(not stage_readiness(output.output_files[0], stage)["ready"]
               for stage in ("still", "start_image", "views", "anim"))


@pytest.mark.parametrize("json_mode", [False, True])
def test_blind_export_marking_refuses_without_traceback(tmp_path, json_mode):
    from spritewarden.learning.db import LearningDB

    db_path = tmp_path / "learning.db"
    result = subprocess.run(
        [sys.executable, "-m", "spritewarden.cli", "recipes", "--mark-tips-exported",
         *(["--json"] if json_mode else [])],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "SPRITEWARDEN_LEARNING_DB": str(db_path)},
    )
    assert result.returncode == 1
    text = result.stdout + result.stderr
    assert "verified" in text
    assert "Traceback" not in text
    assert "Not marked exported:" in text
    assert LearningDB(db_path).list_tip_exports() == []


@pytest.mark.parametrize("execution", [None, "unknown", "refused", "completed"])
def test_failed_attempts_do_not_invent_output_rows(tmp_path, execution):
    from spritewarden.repo.provenance import register_generation

    catalog = AssetCatalog(tmp_path / "catalog.db")
    existing = tmp_path / "existing.png"
    Image.new("RGBA", (16, 16), (1, 2, 3, 255)).save(existing)
    recipe = GenerationRecipe(provider="spritecook", operation="generate", prompt="gear",
                              size=32, canvas_size=32, perspective="top-down")
    # An uncompleted attempt cannot claim existing bytes; a completed attempt
    # with only a missing output cannot invent an artifact either.
    files = [str(tmp_path / "missing.png")] if execution == "completed" else [str(existing)]
    output = GenerationOutput(success=False, provider="spritecook", output_files=files,
                              metadata={"execution": execution})
    assert register_generation(catalog, recipe, output) == []
    assert catalog.search("", limit=50) == []


def test_successful_registration_does_not_claim_human_approval(tmp_path):
    from spritewarden.repo.provenance import register_generation

    catalog = AssetCatalog(tmp_path / "catalog.db")
    path = tmp_path / "candidate.png"
    Image.new("RGBA", (16, 16), (1, 2, 3, 255)).save(path)
    recipe = GenerationRecipe(provider="spritecook", operation="generate", prompt="gear",
                              size=32, canvas_size=32, perspective="top-down")
    output = GenerationOutput(success=True, provider="spritecook", output_files=[str(path)],
                              metadata={"execution": "completed", "validation": "passed"})
    ids = register_generation(catalog, recipe, output)
    provenance = catalog.get_asset(ids[0])["provenance"]
    assert provenance["validation"] == "passed"
    assert provenance["readiness"] == "needs_review"
    assert provenance["human_decision"] == "pending"

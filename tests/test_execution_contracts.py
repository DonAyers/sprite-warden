"""Offline execution-boundary regressions; never call paid services."""
import json
import os
import socket
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

from spritewarden.router.playbook import determine_fallback_recipe, GenerationRecipe
from spritewarden.router.jev import JevClient, DecisionOutcome
from spritewarden.router.recipes import registry


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("NETWORK DISABLED")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setenv("SPRITEWARDEN_OFFLINE", "1")
    monkeypatch.setenv("SPRITEWARDEN_NO_LEARN", "1")


@pytest.fixture
def work(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="execution-contracts-", dir=os.environ.get("TMPDIR")) as folder:
        root = Path(folder)
        monkeypatch.setenv("SPRITEWARDEN_DB", str(root / "catalog.db"))
        monkeypatch.setenv("SPRITEWARDEN_LEARNING_DB", str(root / "learning.db"))
        yield root
        import gc
        gc.collect()  # release SQLite cycles before Windows removes this scratch directory


def recipe(provider="spritefusion", operation="generate", **params):
    return GenerationRecipe(provider=provider, operation=operation, prompt="robot", size=64,
                            canvas_size=64, perspective="low top-down", params=params)


def png(path):
    Image.new("RGBA", (32, 32), (0, 0, 0, 0)).save(path)
    return path


def test_provider_selection_recompiles_the_whole_recipe(monkeypatch):
    client = JevClient(api_key="offline")
    monkeypatch.setattr(client, "ask_decision", lambda **kw: DecisionOutcome(
        question_id="q", decision="sprite_fusion", used_fallback=False))
    result = client.route_generation("robot", asset_type="character")
    entry = registry().get(result.provider, result.operation)
    assert entry is not None, "provider override produced a hybrid recipe"
    assert result.operation == "direction-set"
    assert result.prompt == ""
    assert result.required_gates_before == entry.required_gates_before
    assert "directions" not in result.params
    assert not any("South" in rule for rule in result.rules)


def test_unknown_recipe_fails_closed(work):
    from spritewarden.enforce.core import recipe_gate_reasons
    assert recipe_gate_reasons("spritefusion", "animate_character_v3", {}, work)


def test_source_free_hook_fails_closed(work):
    from spritewarden.enforce.core import check_call
    (work / "spritewarden.json").write_text('{"require_pipeline": true}')
    result = check_call("mcp__pixellab__create_character_v3", {"description": "robot"}, work)
    assert result.decision == "deny"
    assert any("source" in reason for reason in result.reasons)


def test_classification_failure_in_known_namespace_denies(work, monkeypatch):
    from spritewarden.enforce import core
    def broken():
        raise ValueError("broken registry")
    monkeypatch.setattr(core, "load_raw_registry", broken)
    assert core.check_call("mcp__pixellab__create_character_v3", {}, work).decision == "deny"
    assert core.check_call("mcp__unrelated__hello", {}, work).decision == "ignore"


def test_dispatch_preflight_checks_rules_even_without_stage(work):
    from spritewarden.enforce.core import recipe_gate_reasons
    assert recipe_gate_reasons("spritefusion", "edit", {"size": 64}, work)
    assert recipe_gate_reasons("spritecook", "generate", {"colors": ["#ffffff"] * 9}, work)


@pytest.mark.parametrize("operation", ["direction-set", "animate", "edit"])
def test_spritefusion_serializes_input_image_and_operation_fields(work, operation):
    from spritewarden.generator.spritefusion import SpriteFusionDriver
    image = png(work / "source.png")
    r = recipe(operation=operation, input_image=str(image), output_frames=6)
    if operation == "direction-set":
        r.prompt = ""
    body = SpriteFusionDriver(api_key="offline").build_request(r)
    import base64
    assert base64.b64decode(body["inputs"][0]["data_url"].split(",", 1)[1]) == image.read_bytes()
    assert ("size" in body) == (operation == "direction-set")
    if operation == "animate":
        assert body["output_frames"] == 6


def test_spritefusion_does_not_silently_clamp_or_drop_bad_params(work):
    from spritewarden.generator.spritefusion import SpriteFusionDriver
    driver = SpriteFusionDriver(api_key="offline")
    r = recipe(operation="direction-set", input_image=str(png(work / "source.png")))
    r.prompt = ""
    r.size = 96
    with pytest.raises(ValueError, match="size"):
        driver.build_request(r)
    with pytest.raises(ValueError, match="size"):
        driver.build_request(recipe(operation="animate", size=64))
    with pytest.raises(ValueError, match="source"):
        driver.build_request(recipe(operation="edit"))


def test_spritecook_input_image_is_imported(work, monkeypatch):
    from spritewarden.generator.spritecook import SpriteCookDriver
    image = png(work / "source.png")
    driver = SpriteCookDriver(api_key="offline")
    captured = []
    monkeypatch.setattr(driver, "_import_asset", lambda client, headers, path: captured.append(path) or "imported")
    assert driver._resolve_asset_id(recipe("spritecook", "animate", input_image=str(image))) == "imported"
    assert captured == [image]


@pytest.mark.parametrize("provider,operation,status", [
    ("pixellab", "create_character_v3", "import_only"),
    ("spritecook", "generate_character", "advisory"),
    ("spritecook", "generate_character_animations", "advisory"),
    ("openrouter_image", "snap", "advisory"),
    ("spritefusion", "animate_character_v3", "unsupported"),
])
def test_unsupported_workflows_never_reach_driver(work, monkeypatch, provider, operation, status):
    from spritewarden.generator import registry as execution
    driver = Mock()
    monkeypatch.setattr(execution, "get_driver", lambda *a, **kw: driver)
    out = execution.dispatch(recipe(provider, operation), work / "out", project_root=work, register=False, force_ungated=True)
    assert not out.success
    driver.generate.assert_not_called()
    assert execution.capability_status(provider, operation)["status"] == status


def test_success_without_files_is_not_success(work, monkeypatch):
    from spritewarden.generator import registry as execution
    from spritewarden.generator.base import GenerationOutput
    driver = Mock()
    driver.generate.return_value = GenerationOutput(success=True, provider="spritefusion")
    monkeypatch.setattr(execution, "get_driver", lambda *a, **kw: driver)
    out = execution.dispatch(recipe(), work / "out", project_root=work, register=False)
    assert not out.success
    assert "output" in out.error.lower()


def test_required_post_gates_run_on_downloaded_files(work, monkeypatch):
    from spritewarden.generator import registry as execution
    from spritewarden.generator.base import GenerationOutput
    from spritewarden.gates.records import load_record
    image = work / "clipped.png"
    Image.new("RGBA", (32, 32), (255, 0, 0, 255)).save(image)
    before = image.read_bytes()
    driver = Mock()
    driver.generate.return_value = GenerationOutput(success=True, provider="spritefusion", output_files=[str(image)])
    monkeypatch.setattr(execution, "get_driver", lambda *a, **kw: driver)
    out = execution.dispatch(recipe(), work / "out", project_root=work, register=False)
    assert not out.success
    assert out.metadata["execution"] == "completed"
    assert out.metadata["validation"] == "failed"
    assert "clipping" in load_record(image)["gates"]
    assert image.read_bytes() == before


def test_attempt_capture_is_required_before_spend(work, monkeypatch):
    from spritewarden import events
    from spritewarden.generator import registry as execution
    driver = Mock()
    monkeypatch.setattr(execution, "get_driver", lambda *a, **kw: driver)
    calls = []
    monkeypatch.setattr(events, "emit", lambda event_type, **kw: calls.append((event_type, kw)) or None)
    out = execution.dispatch(recipe(), work / "out", project_root=work, register=False)
    assert not out.success
    driver.generate.assert_not_called()
    assert any(event == "attempt.started" for event, _ in calls)
    assert out.metadata["refused"] == "observability"


def test_driver_exception_records_failed_attempt_without_retry(work, monkeypatch):
    from spritewarden import events
    from spritewarden.generator import registry as execution
    driver = Mock()
    driver.generate.side_effect = RuntimeError("connection lost")
    monkeypatch.setattr(execution, "get_driver", lambda *a, **kw: driver)
    calls = []
    monkeypatch.setattr(events, "emit", lambda event_type, **kw: calls.append((event_type, kw)) or "event-id")
    out = execution.dispatch(recipe(), work / "out", project_root=work, register=False)
    assert not out.success
    assert driver.generate.call_count == 1
    assert [event for event, _ in calls] == ["attempt.started", "attempt.failed"]
    assert len({kw["run_id"] for _, kw in calls}) == 1


@pytest.mark.parametrize("state", ["rejected", "stale", "ungated"])
def test_router_does_not_reuse_unapproved_catalog_hit(work, monkeypatch, state):
    from spritewarden.router import PipelineRouter
    from spritewarden.gates.records import write_record, mark_rejected
    image = png(work / "robot.png")
    if state != "ungated":
        write_record(image, {"still": True})
    if state == "rejected":
        mark_rejected(image, by="owner", reason="not this design")
    elif state == "stale":
        Image.new("RGBA", (16, 16)).save(image)
    catalog = Mock()
    catalog.search.return_value = [{"name": "robot", "file_path": str(image)}]
    router = PipelineRouter(catalog=catalog)
    monkeypatch.setattr(router.jev, "route_generation", lambda **kw: recipe())
    assert router.route_request("robot").action != "reuse_existing"


def test_recorded_route_constrains_model_selection(work, monkeypatch):
    from spritewarden.router import PipelineRouter
    (work / "pipeline.json").write_text(json.dumps({"rotation_provider": "spritefusion", "anchor": "anchor.png"}))
    catalog = Mock()
    catalog.search.return_value = []
    router = PipelineRouter(catalog=catalog)
    model = Mock(side_effect=AssertionError("recorded route must not call a model"))
    monkeypatch.setattr(router.jev, "route_generation", model)
    result = router.route_request("robot", pipeline_path=work / "pipeline.json")
    assert result.recipe.provider == "spritefusion"
    assert result.recipe.operation == "direction-set"
    assert result.recipe.params["input_image"] == str(work / "anchor.png")
    model.assert_not_called()


def test_hook_allow_and_deny_emit_but_ignored_does_not(work, monkeypatch):
    from spritewarden import events
    from spritewarden.enforce.core import check_call
    calls = []
    monkeypatch.setattr(events, "emit", lambda event_type, **kw: calls.append((event_type, kw)) or "event-id")
    check_call("mcp__spritecook__generate_game_art", {"prompt": "robot"}, work)
    check_call("mcp__pixellab__create_character_v3", {}, work)
    check_call("Bash", {"command": "git status"}, work)
    assert [name for name, _ in calls] == ["hook.allowed", "hook.denied"]


def test_native_workflows_cannot_use_generic_driver_directly(work):
    from spritewarden.generator.spritecook import SpriteCookDriver
    from spritewarden.generator.pixellab import PixelLabDriver
    from spritewarden.generator.openrouter_image import OpenRouterImageDriver
    for driver, r in [
        (SpriteCookDriver(api_key="offline"), recipe("spritecook", "generate_character")),
        (PixelLabDriver(api_key="offline"), recipe("pixellab", "create_character_v3", character_id="existing")),
        (OpenRouterImageDriver(api_key="offline"), recipe("openrouter_image", "snap")),
    ]:
        out = driver.generate(r, work)
        assert not out.success and "unsupported" in out.error.lower()


def test_spritecook_serialized_palette_obeys_contract_and_anim_fields():
    from spritewarden.generator.spritecook import SpriteCookDriver
    driver = SpriteCookDriver(api_key="offline")
    body = driver.build_request(recipe("spritecook", "generate"))
    assert len(body["colors"]) <= 8
    body = driver.build_request(recipe("spritecook", "animate", asset_id="a", model="pixel-engine-v1.1",
                                       auto_enhance_prompt=True, frames=6, matte_color="#FF00FF"))
    assert body["model"] == "pixel-engine-v1.1"
    assert body["auto_enhance_prompt"] is True
    assert body["output_frames"] == 6


def test_successful_output_does_not_lose_bytes_if_telemetry_fails(work, monkeypatch):
    from spritewarden import events
    from spritewarden.generator import registry as execution
    from spritewarden.generator.base import GenerationOutput
    from spritewarden.judge.palette import load_palette
    image = Image.new("RGBA", (32, 32))
    from PIL import ImageDraw
    ImageDraw.Draw(image).rectangle((8, 8, 23, 23), fill=tuple(load_palette("luap-40")[0]) + (255,))
    path = work / "candidate.png"
    image.save(path)
    raw = path.read_bytes()
    driver = Mock()
    driver.generate.return_value = GenerationOutput(success=True, provider="spritefusion", output_files=[str(path)])
    monkeypatch.setattr(execution, "get_driver", lambda *a, **kw: driver)
    monkeypatch.setattr(events, "emit", lambda kind, **kw: "id" if kind == "attempt.started" else None)
    out = execution.dispatch(recipe(), work, project_root=work, register=False)
    assert out.success
    assert out.metadata["observability_degraded"]
    assert out.metadata["human_decision"] == "pending"
    assert path.read_bytes() == raw
    assert driver.generate.call_count == 1


def test_dispatch_does_not_validate_one_source_and_send_another(work, monkeypatch):
    from spritewarden.generator import registry as execution
    from spritewarden.gates.records import write_record
    source = png(work / "source.png")
    other = png(work / "other.png")
    write_record(source, {"start_image": True})
    write_record(other, {"start_image": True})
    manifest = work / "art_source" / "spritecook-assets.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"assets": [{"asset_id": "id", "file": str(other)}]}))
    driver = Mock()
    monkeypatch.setattr(execution, "get_driver", lambda *a, **kw: driver)
    out = execution.dispatch(recipe("spritecook", "animate", asset_id="id", input_image=str(source)),
                             work / "out", project_root=work, register=False)
    assert not out.success
    assert "source" in out.error.lower()
    driver.generate.assert_not_called()


def test_spritecook_explicit_palette_is_not_silently_replaced():
    from spritewarden.generator.spritecook import SpriteCookDriver
    driver = SpriteCookDriver(api_key="offline")
    with pytest.raises(ValueError, match="colors"):
        driver.build_request(recipe("spritecook", "generate", colors=["#ffffff"] * 9))
    assert driver.build_request(recipe("spritecook", "generate", colors=["#ffffff"]))["colors"] == ["#ffffff"]


def test_spritefusion_fetch_palette_keeps_original_bytes(work, monkeypatch):
    import io
    from spritewarden.generator.spritefusion import SpriteFusionDriver
    buffer = io.BytesIO()
    Image.new("RGBA", (16, 16), (123, 45, 67, 255)).save(buffer, format="PNG")
    raw = buffer.getvalue()
    response = Mock(status_code=200, content=raw)
    client = Mock()
    client.get.return_value = response
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    monkeypatch.setattr("spritewarden.generator.spritefusion.httpx.Client", lambda **kw: client)
    paths, records = SpriteFusionDriver(api_key="offline")._download_many(
        [{"id": "asset", "url": "https://example.test/sprite.png"}], work, "sweetie-16")
    assert Path(records[0]["raw_file"]).read_bytes() == raw
    assert Path(paths[0]) != Path(records[0]["raw_file"])


def test_spritecook_still_source_cannot_be_silently_dropped(work, monkeypatch):
    from spritewarden.generator import registry as execution
    driver = Mock()
    monkeypatch.setattr(execution, "get_driver", lambda *a, **kw: driver)
    out = execution.dispatch(recipe("spritecook", "generate", input_image=str(png(work / "source.png"))),
                             work, project_root=work, register=False)
    assert not out.success
    assert "reference_asset_id" in out.error
    driver.generate.assert_not_called()


def test_driver_lookup_resolves_recipe_provider_aliases():
    from spritewarden.generator.registry import get_driver
    from spritewarden.generator.spritefusion import SpriteFusionDriver
    assert isinstance(get_driver("sf", api_key="offline"), SpriteFusionDriver)


def test_download_directories_do_not_overwrite_prior_attempts(work, monkeypatch):
    from spritewarden.generator import registry as execution
    from spritewarden.generator.base import GenerationOutput
    destinations = []
    driver = Mock()
    def generate(r, destination):
        destinations.append(Path(destination))
        return GenerationOutput(success=False, provider=r.provider, error="synthetic failure")
    driver.generate.side_effect = generate
    monkeypatch.setattr(execution, "get_driver", lambda *a, **kw: driver)
    execution.dispatch(recipe(), work / "out", project_root=work, register=False)
    execution.dispatch(recipe(), work / "out", project_root=work, register=False)
    assert len(set(destinations)) == 2


def test_dispatch_rechecks_input_revision_after_intent_capture(work, monkeypatch):
    from spritewarden import events
    from spritewarden.generator import registry as execution
    from spritewarden.gates.records import write_record
    source = png(work / "source.png")
    write_record(source, {"start_image": True})
    driver = Mock()
    monkeypatch.setattr(execution, "get_driver", lambda *a, **kw: driver)
    def emit(kind, **kw):
        if kind == "attempt.started":
            Image.new("RGBA", (16, 16)).save(source)
        return "event-id"
    monkeypatch.setattr(events, "emit", emit)
    out = execution.dispatch(recipe(operation="animate", input_image=str(source)), work,
                             project_root=work, register=False)
    assert not out.success
    assert "changed" in out.error.lower()
    driver.generate.assert_not_called()


def test_unknown_hook_spending_operation_refuses(work):
    from spritewarden.enforce.core import check_call
    result = check_call("mcp__pixellab__create_mystery_workflow", {}, work)
    assert result.decision == "deny"
    assert any("unsupported" in reason.lower() for reason in result.reasons)


def test_offline_routing_does_not_use_configured_keys(monkeypatch):
    from spritewarden.router.fusion import FusionSynthesizer
    monkeypatch.setenv("SPRITEWARDEN_OFFLINE", "1")
    assert not JevClient(api_key="configured-but-offline").is_available()
    assert not FusionSynthesizer(api_key="configured-but-offline").is_available()


def test_spritefusion_rejects_invalid_or_oversized_rotation_source(work):
    from spritewarden.generator.spritefusion import SpriteFusionDriver
    driver = SpriteFusionDriver(api_key="offline")
    path = work / "source.png"
    path.write_bytes(b"not an image")
    r = recipe(operation="direction-set", input_image=str(path))
    r.prompt = ""
    with pytest.raises(ValueError, match="source"):
        driver.build_request(r)
    Image.new("RGBA", (128, 128)).save(path)
    with pytest.raises(ValueError, match="64"):
        driver.build_request(r)


def test_animation_post_gates_record_source_integrity_without_changing_output(work, monkeypatch):
    from spritewarden.generator import registry as execution
    from spritewarden.generator.base import GenerationOutput
    from spritewarden.gates.records import write_record, load_record
    from spritewarden.judge.palette import load_palette
    from PIL import ImageDraw
    source = work / "source.png"
    image = Image.new("RGBA", (32, 32))
    ImageDraw.Draw(image).rectangle((8, 8, 23, 23), fill=tuple(load_palette("luap-40")[0]) + (255,))
    image.save(source)
    write_record(source, {"start_image": True})
    sheet = Image.new("RGBA", (64, 32))
    sheet.paste(image, (0, 0))
    sheet.paste(image, (32, 0))
    path = work / "sheet.png"
    sheet.save(path)
    before = path.read_bytes()
    driver = Mock()
    driver.generate.return_value = GenerationOutput(success=True, provider="spritefusion", output_files=[str(path)])
    monkeypatch.setattr(execution, "get_driver", lambda *a, **kw: driver)
    r = recipe(operation="animate", input_image=str(source), cell=32, output_frames=2)
    out = execution.dispatch(r, work / "out", project_root=work, register=False)
    assert out.metadata["execution"] == "completed"
    record = load_record(path)
    assert {"loop_smoothness", "animation_anomalies", "frame_integrity", "palette_adherence"} <= record["gates"].keys()
    assert record["params"]["source"] == str(source.resolve())
    assert path.read_bytes() == before

"""Single spending boundary shared by the registry and workflow facade."""
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from .base import GenerationOutput
from .contracts import capability_status, source_paths, SOURCE_KEYS
from .spritefusion import SpriteFusionDriver


def execute(recipe, output_dir, *, get_driver, force_ungated=False, project_root=None,
            register=True, catalog=None, game="global", character_id=None, run_id=None, **kwargs):
    from .. import events
    from ..enforce import find_project_root
    from ..enforce.core import recipe_rule_reasons, recipe_gate_reasons, pipeline_reasons, load_raw_registry, _Manifest
    from ..enforce.config import load_config
    from ..router.recipes import registry
    from .validation import validate_outputs

    root = Path(project_root) if project_root else find_project_root(Path.cwd())
    run_id = run_id or events.current_run_id() or str(uuid4())
    common = {"provider": recipe.provider, "operation": recipe.operation, "transport": "rest"}

    def emit(kind, payload):
        try:
            return events.emit(kind, project_root=root, run_id=run_id, payload={**common, **payload})
        except Exception:
            return None

    def refused(reason, category="contract", reasons=None):
        captured = emit("attempt.refused", {"reason": reason, "category": category})
        return GenerationOutput(success=False, provider=recipe.provider, error=f"Refused: {reason}",
                                metadata={"run_id": run_id, "refused": category,
                                          "reasons": reasons or [reason], "observability_degraded": captured is None})

    try:
        capability = capability_status(recipe.provider, recipe.operation)
        if not capability["executable"]:
            prefix = "No generator driver registered. " if registry().canonical_provider(recipe.provider) is None else ""
            return refused(f"{prefix}Unsupported execution {recipe.provider}/{recipe.operation}: {capability['reason']}", capability["status"])
        entry = registry().get(recipe.provider, recipe.operation)
        common.update(provider=capability["provider"], operation=entry.operation)
        compiled = recipe.model_copy(deep=True)
        compiled.operation = entry.operation
        compiled.required_gates_before = list(entry.required_gates_before)
        compiled.suggested_gates = list(entry.required_gates_after)
        paths = source_paths(compiled.params, root)
        for key in SOURCE_KEYS:
            compiled.params.pop(key, None)
        if paths:
            compiled.params["inputs"] = [str(path) for path in paths]
        cfg = load_config(root)
        if capability["provider"] == "spritecook":
            asset_id = compiled.params.get("asset_id") or compiled.params.get("edit_asset_id") or compiled.params.get("reference_asset_id")
            resolved = _Manifest(cfg).asset_file(asset_id) if asset_id else None
            if asset_id and paths and (resolved is None or len(paths) != 1 or resolved.resolve() != paths[0].resolve()):
                raise ValueError("source image conflicts with the registered provider asset_id; validate the image actually sent")
            if resolved and not paths:
                paths = [resolved]
        from ..workflow_state import source_revision_reasons
        review_reasons = [reason for path in paths for reason in source_revision_reasons(root, path)]
        if review_reasons:
            return refused(" | ".join(review_reasons), "revision", review_reasons)
        args = {**compiled.params, "prompt": compiled.prompt}
        # Keep preflight evidence refusals stable; --force-ungated never waives
        # the later source serializer, provider rules, or recorded route checks.
        reasons = recipe_gate_reasons(compiled.provider, compiled.operation, args, root)
        if reasons and not force_ungated:
            return refused(f"{compiled.provider}/{compiled.operation} requires gate(s) {', '.join(entry.required_gates_before) or 'on its input'}: " + " | ".join(reasons) + " (--force-ungated overrides evidence only, never missing sources or contracts)", "ungated_input", reasons)
        if capability["provider"] == "spritefusion":
            body = SpriteFusionDriver(api_key="validation-only").build_request(compiled)
            args.update({k: v for k, v in body.items() if k != "inputs"})
        elif capability["provider"] == "spritecook":
            from .spritecook import SpriteCookDriver
            if paths and entry.operation != "animate_game_art" and not (compiled.params.get("reference_asset_id") or compiled.params.get("edit_asset_id")):
                raise ValueError("SpriteCook still inputs require reference_asset_id (import explicitly); local references are not silently dropped")
            body = SpriteCookDriver(api_key="validation-only").build_request(compiled, asset_id=compiled.params.get("asset_id") or ("pending-import" if paths else None))
            args.update({key: value for key, value in body.items() if key != "asset_id"})
            if entry.operation == "edit" and not compiled.params.get("edit_asset_id"):
                raise ValueError("SpriteCook edit requires edit_asset_id; native ids are not invented")
            if entry.enforce.get("stage") == "animation" and not paths:
                raise ValueError("Animation requires a registered local source for required post-gates")
        elif paths:
            raise ValueError("OpenRouter reference-image serialization is not implemented")
        hard_reasons = recipe_rule_reasons(compiled.provider, compiled.operation, args, root)
        for path in paths:
            hard_reasons += pipeline_reasons(path, compiled.provider, entry.enforce.get("stage"), cfg,
                                             load_raw_registry(), call_args=args, source_kind="file")
        if hard_reasons:
            return refused(" | ".join(hard_reasons), "contract", hard_reasons)
        reasons = recipe_gate_reasons(compiled.provider, compiled.operation, args, root)
        if reasons and not force_ungated:
            return refused(" | ".join(reasons), "ungated_input", reasons)
        driver = get_driver(compiled.provider, **kwargs)
        if driver is None or not driver.is_configured():
            return refused(f"Provider '{compiled.provider}' is not configured (missing API key)", "configuration")
        fingerprint = hashlib.sha256(json.dumps(compiled.model_dump(), sort_keys=True, default=str).encode()).hexdigest()
        source_revisions = [{"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths]
    except Exception as exc:
        return refused(f"{type(exc).__name__}: {exc}")

    if emit("attempt.started", {"request_fingerprint": fingerprint, "sources": source_revisions,
                                "force_ungated": bool(reasons), "overridden_reasons": reasons if force_ungated else []}) is None:
        return GenerationOutput(success=False, provider=recipe.provider,
                                error="Refused: attempt intent could not be durably captured; no provider request made",
                                metadata={"run_id": run_id, "refused": "observability", "observability_degraded": True})
    try:
        for revision in source_revisions:
            if hashlib.sha256(Path(revision["path"]).read_bytes()).hexdigest() != revision["sha256"]:
                return refused("source image changed after validation; no provider request made", "stale_input")
        review_reasons = [reason for path in paths for reason in source_revision_reasons(root, path)]
        if review_reasons:
            return refused(" | ".join(review_reasons), "revision", review_reasons)
        # A new artifact directory for every attempt, including two calls in one run.
        attempt_dir = Path(output_dir) / str(uuid4())
        attempt_dir.mkdir(parents=True, exist_ok=False)
        output = driver.generate(compiled, attempt_dir)
        if not isinstance(output, GenerationOutput):
            raise TypeError("Driver did not return GenerationOutput")
    except Exception as exc:
        output = GenerationOutput(success=False, provider=compiled.provider,
                                  error=f"{type(exc).__name__}: {exc}", metadata={"execution": "unknown"})
    output.metadata["run_id"] = run_id
    if reasons:
        output.metadata["forced_ungated"] = reasons
    if output.success:
        output.metadata["execution"] = "completed"
        try:
            output = validate_outputs(compiled, output, entry, paths)
        except Exception as exc:
            output.success = False
            output.error = f"Required output validation: {type(exc).__name__}: {exc}"
            output.metadata.update(validation="failed", readiness="blocked")
    else:
        output.metadata.setdefault("execution", "unknown")
    # Downloads remain inspectable when checks fail. No automatic retry or approval.
    if register and output.output_files:
        try:
            from ..repo.provenance import register_generation
            if catalog is None:
                from ..repo.catalog import AssetCatalog
                catalog = AssetCatalog()
            output.metadata["catalog_asset_ids"] = register_generation(catalog, compiled, output, game=game, character_id=character_id)
        except Exception as exc:
            output.metadata["catalog_error"] = f"{type(exc).__name__}: {exc}"
    kind = "attempt.completed" if output.metadata.get("execution") == "completed" else "attempt.failed"
    captured = emit(kind, {"success": output.success, "execution": output.metadata.get("execution"),
                           "validation": output.metadata.get("validation"), "outputs": output.output_files,
                           "provider_asset_ids": output.metadata.get("asset_ids", []), "cost_usd": output.cost_usd,
                           "error": output.error})
    if captured is None:
        output.metadata["observability_degraded"] = True
        output.metadata["warning"] = "Outcome capture failed; provider may have completed. Do not retry blindly."
    return output

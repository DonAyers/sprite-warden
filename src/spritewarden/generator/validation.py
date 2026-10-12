"""Post-generation checks are diagnostics, never implicit human approval."""
from pathlib import Path
from PIL import Image

from ..gates.records import write_record
from ..judge import evaluate_still, evaluate_animation
from ..judge.views import check_direction_set, canonical_facing


def validate_outputs(recipe, output, entry, sources):
    if not output.output_files:
        raise ValueError("Provider returned no output files")
    required = set(entry.required_gates_after)
    stage = entry.enforce.get("stage")
    records = []
    failures = []
    for filename in output.output_files:
        path = Path(filename)
        with Image.open(path) as raw:
            raw.load()
            image = raw.convert("RGBA")
        if not image.getbbox():
            raise ValueError(f"Empty output image: {path}")
        params = {"provider": recipe.provider, "operation": recipe.operation,
                  "required_evidence": sorted(required), "run_id": output.metadata.get("run_id")}
        if stage == "animation":
            if not sources:
                raise ValueError("Animation output validation requires a local source still")
            cell = int(recipe.params.get("cell") or recipe.size)
            if cell <= 0 or image.width % cell or image.height % cell or image.width * image.height // cell ** 2 < 2:
                raise ValueError("Animation output dimensions do not contain complete frames at the declared cell size")
            score = evaluate_animation(image, cell=cell, source=sources[0],
                                       palette_source=recipe.palette_id,
                                       is_walk="walk_mechanics" in required,
                                       locomotion=recipe.params.get("locomotion", "legs"))
            gates = {gate.name: gate for gate in score.gates}
            if "palette_consistency" in gates:
                gates["palette_adherence"] = gates["palette_consistency"]
            params.update(command="judge-anim", source=str(sources[0]), cell=cell)
            umbrella = "anim"
        elif stage == "rotation":
            order = recipe.params.get("direction_order")
            cell = int(recipe.params.get("cell") or recipe.size)
            if not isinstance(order, list) or len(order) != 8 or len({canonical_facing(f) for f in order}) != 8 or any(canonical_facing(f) is None for f in order):
                raise ValueError("Direction output validation requires explicit direction_order (all eight facings); layout is not guessed")
            if image.size != (cell * 8, cell):
                raise ValueError("Direction output must be a declared eight-cell horizontal strip")
            frames = {name: image.crop((i * cell, 0, (i + 1) * cell, cell)) for i, name in enumerate(order)}
            anchor = Image.open(sources[0]).convert("RGBA") if sources else None
            direction = check_direction_set(frames, anchor=anchor, palette=recipe.palette_id)
            gates = {"direction_set": direction}
            for frame in frames.values():
                score = evaluate_still(frame, palette_source=recipe.palette_id)
                for gate in score.gates:
                    if gate.name not in gates or not gate.passed:
                        gates[gate.name] = gate
            params.update(command="judge-views", anchor=str(sources[0]) if sources else None,
                          direction_order=order, cell=cell)
            umbrella = "views"
            # Native pixel/set diagnostics cannot replace the required identity comparison.
            failures.append(f"{path.name}: direction identity comparison requires review/vision; not promoted")
        else:
            score = evaluate_still(image, palette_source=recipe.palette_id,
                                   start_image="start_image" in required,
                                   verify_symmetry="symmetry" in required)
            gates = {gate.name: gate for gate in score.gates}
            params["command"] = "judge-still"
            umbrella = "still"
        missing = sorted(required - gates.keys())
        bad = [name for name, gate in gates.items() if not gate.passed]
        failures.extend(f"{path.name}: {name} failed" for name in bad)
        failures.extend(f"{path.name}: required {name} unavailable" for name in missing)
        verdicts = dict(gates)
        verdicts[umbrella] = not bad and not missing and stage != "rotation"
        record = write_record(path, verdicts, params=params)
        records.append(str(record))
    output.metadata.update(validation="failed" if failures else "passed", gate_records=records,
                           readiness="blocked" if failures else "needs_review", human_decision="pending")
    if failures:
        output.success = False
        output.error = "Required output validation: " + " | ".join(failures)
    return output

"""Vision Model Judge & Jev Orchestrator: visual smell-test, camera elevation, identity match,
and reference-aware comparison (anchor vs candidate on one board).

Offline rule: when no OpenRouter key is available the smell test is skipped
with a stub, but a reference comparison is reported as ``unavailable`` and its
gate FAILS. A gate that cannot look must never accept.
"""

import base64
import io
import json
import os
from pathlib import Path
from typing import Any
import httpx
from PIL import Image, ImageDraw
from pydantic import BaseModel, Field

from ..router.jev import JevClient, _get_openrouter_key
from .models import GateResult, StillScorecard

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_VISION_MODEL = "google/gemini-2.5-flash"
OFFLINE_ENV = "SPRITEWARDEN_OFFLINE"
BOARD_BG = (109, 161, 138)  # #6da18a


class VisionObservation(BaseModel):
    """Raw observations extracted by a multimodal vision model viewing an image."""
    model_used: str
    camera_elevation_degrees: int | None = None
    perspective_match: bool = True
    identity_features_present: list[str] = Field(default_factory=list)
    identity_features_missing: list[str] = Field(default_factory=list)
    anomalies: list[str] = Field(default_factory=list)
    visual_smell_test_notes: str = ""
    cost_usd: float | None = None


class ReferenceDifference(BaseModel):
    kind: str = "other"  # added | removed | moved | resized | recolored | other
    part: str = ""
    detail: str = ""


class ReferenceComparison(BaseModel):
    """Structured diff between an approved anchor and a candidate view/frame."""
    status: str  # match | mismatch | unavailable | error
    model_used: str = ""
    differences: list[ReferenceDifference] = Field(default_factory=list)
    reference_hands: dict[str, str] = Field(default_factory=dict)
    candidate_hands: dict[str, str] = Field(default_factory=dict)
    reference_camera_elevation_degrees: int | None = None
    candidate_camera_elevation_degrees: int | None = None
    perspective_match: bool | None = None
    same_character: bool | None = None
    verdict: str = ""
    notes: str = ""
    cost_usd: float | None = None

    def anomalies(self) -> list[ReferenceDifference]:
        return [d for d in self.differences if d.kind in ("added", "removed", "moved", "resized")]


class OrchestratedVerdict(BaseModel):
    """Final decision produced by Jev synthesizing deterministic metrics and vision observations."""
    passed: bool
    confidence: float
    production_ready_prob: float
    recommended_action: str  # "accept" | "palette_remap" | "retime_stalls" | "inpaint_part" | "reroll"
    quality_score: float
    vision_observation: VisionObservation | None = None
    reasoning_summary: str


def _board(image: Image.Image, zoom: int) -> Image.Image:
    img = image.convert("RGBA")
    box = img.getbbox()
    if box:
        img = img.crop(box)
    w, h = img.size
    return img.resize((w * zoom, h * zoom), Image.NEAREST)


def _to_data_url(board: Image.Image) -> str:
    buf = io.BytesIO()
    board.convert("RGB").save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def prepare_sprite_for_vision(
    image: Image.Image,
    zoom: int = 6,
    background_color: tuple[int, int, int] = BOARD_BG,
) -> str:
    """Enlarge a sprite nearest-neighbour onto a flat board so vision encoders read pixels cleanly.

    Returns base64 data URL.
    """
    scaled = _board(image, zoom)
    margin = zoom * 4
    board = Image.new("RGBA", (scaled.width + 2 * margin, scaled.height + 2 * margin), background_color + (255,))
    board.alpha_composite(scaled, (margin, margin))
    return _to_data_url(board)


def prepare_pair_for_vision(
    reference: Image.Image,
    candidate: Image.Image,
    zoom: int = 6,
    background_color: tuple[int, int, int] = BOARD_BG,
    labels: tuple[str, str] = ("REFERENCE", "CANDIDATE"),
) -> Image.Image:
    """Anchor and candidate side by side on one board at the same zoom, labelled, divided."""
    ref = _board(reference, zoom)
    cand = _board(candidate, zoom)
    margin = zoom * 4
    label_h = 14
    height = max(ref.height, cand.height) + 2 * margin + label_h
    width = ref.width + cand.width + 3 * margin + 2  # divider
    board = Image.new("RGBA", (width, height), background_color + (255,))
    draw = ImageDraw.Draw(board)
    # Draw both baselines on the same row so a height/ground difference is visible.
    bottom = height - margin
    board.alpha_composite(ref, (margin, bottom - ref.height))
    x2 = ref.width + 2 * margin + 2
    board.alpha_composite(cand, (x2, bottom - cand.height))
    divider_x = ref.width + margin + margin // 2
    draw.line([(divider_x, 0), (divider_x, height)], fill=(30, 30, 30, 255), width=2)
    draw.text((margin, 2), labels[0], fill=(20, 20, 20, 255))
    draw.text((x2, 2), labels[1], fill=(20, 20, 20, 255))
    return board


def reference_gate(comparison: ReferenceComparison, facing: str | None = None) -> GateResult:
    """Turn a comparison into a hard gate: anomalies, perspective mismatch and 'unavailable' all fail."""
    where = f" ({facing})" if facing else ""
    if comparison.status == "unavailable":
        return GateResult(
            name="vision_reference", passed=False, score=0.0,
            summary="Reference comparison unavailable: no vision model (OPENROUTER_API_KEY) -- not accepted",
            errors=["unavailable: a reference comparison cannot be skipped; set OPENROUTER_API_KEY or review by eye and record it"],
            metrics={"status": "unavailable"},
        )
    if comparison.status == "error":
        return GateResult(
            name="vision_reference", passed=False, score=0.0,
            summary=f"Reference comparison failed: {comparison.notes}",
            errors=[f"error: {comparison.notes}"],
            metrics={"status": "error", "model": comparison.model_used},
        )
    errors: list[str] = []
    for d in comparison.anomalies():
        errors.append(f"{d.kind}: {d.part} {('- ' + d.detail) if d.detail else ''}".strip())
    if comparison.perspective_match is False:
        errors.append(
            f"perspective: candidate camera {comparison.candidate_camera_elevation_degrees} deg vs reference "
            f"{comparison.reference_camera_elevation_degrees} deg"
        )
    if comparison.same_character is False:
        errors.append("identity: the model does not read the candidate as the same character")
    if comparison.reference_hands and comparison.candidate_hands:
        for hand in ("left", "right"):
            r = (comparison.reference_hands.get(hand) or "").strip().lower()
            c = (comparison.candidate_hands.get(hand) or "").strip().lower()
            if r and c and r not in ("none", "empty", "nothing") and r != c and not (r in c or c in r):
                errors.append(f"hands: {hand} hand holds '{c}' but the reference holds '{r}'")
    if comparison.status == "mismatch" and not errors:
        errors.append(f"mismatch: {comparison.verdict or comparison.notes or 'model reported a mismatch'}")
    passed = not errors
    other = [d for d in comparison.differences if d not in comparison.anomalies()]
    return GateResult(
        name="vision_reference",
        passed=passed,
        score=1.0 if passed else max(0.0, 1.0 - 0.25 * len(errors)),
        summary=(f"Matches the reference{where}: {comparison.notes or comparison.verdict}" if passed
                 else f"Differs from the reference{where}: {errors[0]}"),
        errors=errors,
        warnings=[f"{d.kind}: {d.part} {d.detail}".strip() for d in other],
        metrics={
            "status": comparison.status,
            "model": comparison.model_used,
            "differences": len(comparison.differences),
            "anomalies": len(comparison.anomalies()),
            "perspective_match": comparison.perspective_match,
            "same_character": comparison.same_character,
            "reference_hands": comparison.reference_hands,
            "candidate_hands": comparison.candidate_hands,
            "cost_usd": comparison.cost_usd,
        },
        details={"comparison": comparison.model_dump()},
    )


class VisionJudge:
    """Orchestrates visual judging: prepares sprites, calls vision models, and runs Jev decisions."""

    def __init__(
        self,
        api_key: str | None = None,
        vision_model: str = DEFAULT_VISION_MODEL,
        timeout: float = 30.0,
        offline: bool | None = None,
    ):
        if offline is None:
            offline = os.environ.get(OFFLINE_ENV, "").strip().lower() in ("1", "true", "yes", "on")
        self.offline = offline
        self.api_key = "" if offline else (api_key or _get_openrouter_key())
        self.vision_model = vision_model
        self.timeout = timeout
        self.jev = JevClient(api_key=self.api_key)

    def is_available(self) -> bool:
        return bool(self.api_key) and not self.offline

    def _chat_json(self, content: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float | None, str | None]:
        """POST one multimodal message; return (parsed JSON, cost, error)."""
        payload = {
            "model": self.vision_model,
            "messages": [{"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    OPENROUTER_CHAT_URL,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
            if resp.status_code != 200:
                return None, None, f"HTTP {resp.status_code}: {resp.text[:200]}"
            data = resp.json()
            raw_text = data["choices"][0]["message"]["content"]
            parsed = json.loads(raw_text)
            cost = (data.get("usage") or {}).get("cost")
            return parsed, cost, None
        except Exception as e:  # noqa: BLE001
            return None, None, str(e)

    def view_and_inspect(
        self,
        image: Image.Image,
        prompt_description: str = "",
        expected_perspective: str = "low top-down (approx 20-30 degrees)",
        reference_image: Image.Image | None = None,
        expected_features: list[str] | None = None,
    ) -> VisionObservation:
        """Pass the enlarged sprite to a multimodal vision model to inspect visual characteristics."""
        if not self.is_available():
            return VisionObservation(
                model_used="offline_stub",
                visual_smell_test_notes="Vision model inspection skipped (OPENROUTER_API_KEY not configured)",
            )

        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": f"""You are an expert game artist inspecting a 2D pixel art sprite.
Context:
- Expected subject: {prompt_description or 'game character'}
- Expected camera perspective: {expected_perspective}
- Expected features: {', '.join(expected_features or ['consistent robotic or character shape'])}

Analyze the image carefully. You must return a single JSON object with these exact keys:
{{
  "camera_elevation_degrees": integer (0 = side view, 90 = top down),
  "perspective_match": boolean (true if it matches {expected_perspective}),
  "identity_features_present": [string],
  "identity_features_missing": [string],
  "anomalies": [string] (e.g. extra limbs, severed borders, melted pixels, floating fragments),
  "visual_smell_test_notes": string (1-2 sentences)
}}
Only return valid JSON."""
            },
            {"type": "image_url", "image_url": {"url": prepare_sprite_for_vision(image)}},
        ]

        if reference_image:
            content.append({"type": "text", "text": "Reference image of approved character to match:"})
            content.append({"type": "image_url", "image_url": {"url": prepare_sprite_for_vision(reference_image)}})

        parsed, cost, err = self._chat_json(content)
        if err:
            return VisionObservation(model_used=self.vision_model, visual_smell_test_notes=f"Vision inspection failed: {err}")
        if not parsed:
            return VisionObservation(model_used=self.vision_model, visual_smell_test_notes="Vision model failed to parse")
        return VisionObservation(
            model_used=self.vision_model,
            camera_elevation_degrees=parsed.get("camera_elevation_degrees"),
            perspective_match=parsed.get("perspective_match", True),
            identity_features_present=parsed.get("identity_features_present", []),
            identity_features_missing=parsed.get("identity_features_missing", []),
            anomalies=parsed.get("anomalies", []),
            visual_smell_test_notes=parsed.get("visual_smell_test_notes", ""),
            cost_usd=cost,
        )

    def compare_to_reference(
        self,
        candidate: Image.Image,
        reference: Image.Image,
        *,
        facing: str | None = None,
        description: str = "",
        expected_perspective: str = "low top-down (approx 20-30 degrees)",
        zoom: int = 6,
    ) -> ReferenceComparison:
        """Anchor + candidate on one board; ask for a structured diff and a verdict.

        Offline (no key) returns status ``unavailable``: the caller's gate must fail, never accept.
        """
        if not self.is_available():
            return ReferenceComparison(
                status="unavailable",
                model_used="offline_stub",
                notes="No vision model available (OPENROUTER_API_KEY not configured or SPRITEWARDEN_OFFLINE set)",
            )

        facing_text = (
            f"The candidate is the same character turned to face {facing}; parts may be hidden by the turn, "
            f"but nothing may be added, and held tools must stay in the same hands."
            if facing else "The candidate should show the same character from the same camera."
        )
        prompt = f"""You are a pixel-art art director comparing two sprites on one board.
LEFT = REFERENCE (the approved anchor). RIGHT = CANDIDATE.
Subject: {description or 'game character'}. Expected camera: {expected_perspective}. {facing_text}

List EVERY difference from the reference. Return a single JSON object with exactly these keys:
{{
  "differences": [{{"kind": "added|removed|moved|resized|recolored|other", "part": string, "detail": string}}],
  "reference_hands": {{"left": string, "right": string}} (what each hand holds, 'none' if empty),
  "candidate_hands": {{"left": string, "right": string}},
  "reference_camera_elevation_degrees": integer (0 = side view, 90 = top down),
  "candidate_camera_elevation_degrees": integer,
  "perspective_match": boolean (same camera elevation within ~15 degrees and same projection),
  "same_character": boolean,
  "verdict": "match" or "mismatch",
  "notes": string (1-2 sentences)
}}
Rules: a part present in one image and absent in the other is 'added' or 'removed' (floating fragments,
orbs, extra limbs count). A tool in a different hand is 'moved'. Report 'match' only when there are no
added/removed/moved/resized differences and the perspective matches. Only return valid JSON."""
        board = prepare_pair_for_vision(reference, candidate, zoom=zoom)
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": _to_data_url(board)}},
        ]
        parsed, cost, err = self._chat_json(content)
        if err or not isinstance(parsed, dict):
            return ReferenceComparison(status="error", model_used=self.vision_model, notes=err or "unparseable response")

        diffs = []
        for d in parsed.get("differences", []) or []:
            if isinstance(d, dict):
                diffs.append(ReferenceDifference(
                    kind=str(d.get("kind", "other")).lower().strip(),
                    part=str(d.get("part", "")),
                    detail=str(d.get("detail", "")),
                ))
            elif isinstance(d, str):
                diffs.append(ReferenceDifference(kind="other", part=d))
        verdict = str(parsed.get("verdict", "")).lower().strip()
        status = "match" if verdict == "match" else "mismatch"
        return ReferenceComparison(
            status=status,
            model_used=self.vision_model,
            differences=diffs,
            reference_hands={k: str(v) for k, v in (parsed.get("reference_hands") or {}).items()},
            candidate_hands={k: str(v) for k, v in (parsed.get("candidate_hands") or {}).items()},
            reference_camera_elevation_degrees=parsed.get("reference_camera_elevation_degrees"),
            candidate_camera_elevation_degrees=parsed.get("candidate_camera_elevation_degrees"),
            perspective_match=parsed.get("perspective_match"),
            same_character=parsed.get("same_character"),
            verdict=verdict,
            notes=str(parsed.get("notes", "")),
            cost_usd=cost,
        )

    def orchestrate_judgment(
        self,
        still_scorecard: StillScorecard,
        image: Image.Image,
        prompt_description: str = "",
        expected_perspective: str = "low top-down",
        reference_image: Image.Image | None = None,
        expected_features: list[str] | None = None,
    ) -> OrchestratedVerdict:
        """Run vision model inspection and synthesize final judgment through Jev Decision Model.

        When the vision model actually looked, reported anomalies or a perspective
        mismatch hard-fail the verdict regardless of Jev's probability.
        """
        # 1. Inspect visually
        observation = self.view_and_inspect(
            image=image,
            prompt_description=prompt_description,
            expected_perspective=expected_perspective,
            reference_image=reference_image,
            expected_features=expected_features,
        )
        looked = observation.model_used != "offline_stub" and not observation.visual_smell_test_notes.startswith("Vision inspection failed")

        # 2. Package all state (deterministic gates + vision observations) for Jev
        state = {
            "deterministic_passed": still_scorecard.passed,
            "deterministic_score": still_scorecard.score,
            "gate_failures": [g.name for g in still_scorecard.gates if not g.passed],
            "vision_perspective_match": observation.perspective_match,
            "vision_camera_elevation": observation.camera_elevation_degrees,
            "vision_anomalies": observation.anomalies,
            "missing_features": observation.identity_features_missing,
        }

        # 3. Call Jev for the final decision
        noul_res = self.jev.ask_decision(
            state=state,
            question="Is this pixel art asset ready for production use without critical defects?",
            primitive="noul",
            question_id="production_ready",
        )
        prod_ready_prob = float(noul_res.decision) if isinstance(noul_res.decision, (int, float)) else 0.8

        action_criteria = {
            "accept": "Asset passes all gates and visual inspections cleanly",
            "palette_remap": "Asset has off-palette colors that can be mechanically snapped without visual harm",
            "inpaint_part": "Asset has one specific defective feature that should be masked and inpainted",
            "reroll": "Asset has severe perspective mismatch, mutilated silhouette, or failed multiple gates",
        }
        choice_res = self.jev.ask_decision(
            state=state,
            question="What is the optimal remediation action for this asset?",
            primitive="choice",
            options=action_criteria,
            question_id="remediation_action",
        )
        recommended_action = str(choice_res.decision)

        hard_fail: list[str] = []
        if looked:
            if observation.anomalies:
                hard_fail.append("anomalies: " + "; ".join(observation.anomalies[:4]))
            if not observation.perspective_match:
                hard_fail.append(f"perspective mismatch (camera {observation.camera_elevation_degrees} deg vs {expected_perspective})")
        if hard_fail and recommended_action == "accept":
            recommended_action = "inpaint_part" if observation.perspective_match else "reroll"

        passed = (prod_ready_prob >= 0.7) and still_scorecard.passed and not hard_fail
        if hard_fail:
            summary = f"Asset rejected by vision [{recommended_action}]: " + " | ".join(hard_fail)
        elif passed:
            summary = f"Asset approved ({prod_ready_prob * 100:.0f}% confidence): {observation.visual_smell_test_notes}"
        else:
            summary = (f"Asset requires action [{recommended_action}] ({prod_ready_prob * 100:.0f}% readiness): "
                       f"{observation.visual_smell_test_notes or still_scorecard.summary}")

        return OrchestratedVerdict(
            passed=passed,
            confidence=noul_res.confidence,
            production_ready_prob=prod_ready_prob,
            recommended_action=recommended_action,
            quality_score=round(prod_ready_prob * still_scorecard.score, 2),
            vision_observation=observation,
            reasoning_summary=summary,
        )

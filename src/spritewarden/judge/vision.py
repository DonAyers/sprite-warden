"""Vision Model Judge & Jev Orchestrator: visual smell-test, camera elevation, and identity match."""

import base64
import io
import json
from pathlib import Path
from typing import Any
import httpx
from PIL import Image
from pydantic import BaseModel, Field

from ..router.jev import JevClient, _get_openrouter_key
from .models import GateResult, StillScorecard

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_VISION_MODEL = "google/gemini-2.5-flash"


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


class OrchestratedVerdict(BaseModel):
    """Final decision produced by Jev synthesizing deterministic metrics and vision observations."""
    passed: bool
    confidence: float
    production_ready_prob: float
    recommended_action: str  # "accept" | "palette_remap" | "retime_stalls" | "inpaint_part" | "reroll"
    quality_score: float
    vision_observation: VisionObservation | None = None
    reasoning_summary: str


def prepare_sprite_for_vision(
    image: Image.Image,
    zoom: int = 6,
    background_color: tuple[int, int, int] = (109, 161, 138),  # #6da18a
) -> str:
    """Enlarge a sprite nearest-neighbour onto a flat board so vision encoders read pixels cleanly.

    Returns base64 data URL.
    """
    img = image.convert("RGBA")
    box = img.getbbox()
    if box:
        img = img.crop(box)
    w, h = img.size
    scaled = img.resize((w * zoom, h * zoom), Image.NEAREST)

    margin = zoom * 4
    board = Image.new("RGBA", (scaled.width + 2 * margin, scaled.height + 2 * margin), background_color + (255,))
    board.alpha_composite(scaled, (margin, margin))

    buf = io.BytesIO()
    board.convert("RGB").save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


class VisionJudge:
    """Orchestrates visual judging: prepares sprites, calls vision models, and runs Jev decisions."""

    def __init__(
        self,
        api_key: str | None = None,
        vision_model: str = DEFAULT_VISION_MODEL,
        timeout: float = 30.0,
    ):
        self.api_key = api_key or _get_openrouter_key()
        self.vision_model = vision_model
        self.timeout = timeout
        self.jev = JevClient(api_key=self.api_key)

    def is_available(self) -> bool:
        return bool(self.api_key)

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
  "anomalies": [string] (e.g. extra limbs, severed borders, melted pixels),
  "visual_smell_test_notes": string (1-2 sentences)
}}
Only return valid JSON."""
            },
            {
                "type": "image_url",
                "image_url": {"url": prepare_sprite_for_vision(image)},
            }
        ]

        if reference_image:
            content.append({
                "type": "text",
                "text": "Reference image of approved character to match:"
            })
            content.append({
                "type": "image_url",
                "image_url": {"url": prepare_sprite_for_vision(reference_image)},
            })

        payload = {
            "model": self.vision_model,
            "messages": [{"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    OPENROUTER_CHAT_URL,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    raw_text = data["choices"][0]["message"]["content"]
                    parsed = json.loads(raw_text)
                    cost = (data.get("usage") or {}).get("cost")
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
        except Exception as e:
            return VisionObservation(
                model_used=self.vision_model,
                visual_smell_test_notes=f"Vision inspection failed: {str(e)}",
            )

        return VisionObservation(model_used=self.vision_model, visual_smell_test_notes="Vision model failed to parse")

    def orchestrate_judgment(
        self,
        still_scorecard: StillScorecard,
        image: Image.Image,
        prompt_description: str = "",
        expected_perspective: str = "low top-down",
        reference_image: Image.Image | None = None,
        expected_features: list[str] | None = None,
    ) -> OrchestratedVerdict:
        """Run vision model inspection and synthesize final judgment through Jev Decision Model."""
        # 1. Inspect visually
        observation = self.view_and_inspect(
            image=image,
            prompt_description=prompt_description,
            expected_perspective=expected_perspective,
            reference_image=reference_image,
            expected_features=expected_features,
        )

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
        # Question 1: Is this production ready? (noul)
        noul_res = self.jev.ask_decision(
            state=state,
            question="Is this pixel art asset ready for production use without critical defects?",
            primitive="noul",
            question_id="production_ready",
        )
        prod_ready_prob = float(noul_res.decision) if isinstance(noul_res.decision, (int, float)) else 0.8

        # Question 2: What is the recommended remediation action? (choice)
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

        passed = (prod_ready_prob >= 0.7) and still_scorecard.passed
        summary = (
            f"Asset approved ({prod_ready_prob * 100:.0f}% confidence): {observation.visual_smell_test_notes}"
            if passed
            else f"Asset requires action [{recommended_action}] ({prod_ready_prob * 100:.0f}% readiness): {observation.visual_smell_test_notes or still_scorecard.summary}"
        )

        return OrchestratedVerdict(
            passed=passed,
            confidence=noul_res.confidence,
            production_ready_prob=prod_ready_prob,
            recommended_action=recommended_action,
            quality_score=round(prod_ready_prob * still_scorecard.score, 2),
            vision_observation=observation,
            reasoning_summary=summary,
        )

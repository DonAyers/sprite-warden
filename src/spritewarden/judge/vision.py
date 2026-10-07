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
import re
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


class PartNote(BaseModel):
    """One part the model points at: what, where on the sprite, roughly how big, and why."""
    part: str = ""
    location: str = ""
    size: str = ""
    detail: str = ""

    def text(self) -> str:
        bits = [self.part or "unnamed part"]
        if self.location:
            bits.append(f"at {self.location}")
        if self.size:
            bits.append(f"(~{self.size})")
        if self.detail:
            bits.append(f"- {self.detail}")
        return " ".join(bits)


ELEVATION_TOLERANCE_DEGREES = 15
# A "missing" part whose own explanation is the turn is not missing: it is expected to be hidden.
_OCCLUSION_RE = re.compile(
    r"turned away|facing away|faces away|occlud|hidden|not visible from|out of view|out of sight|"
    r"on the (?:far|other|opposite) side|behind the (?:body|head|torso)|blocked by|obscured",
    re.IGNORECASE,
)


class ReferenceComparison(BaseModel):
    """Structured comparison of a candidate view/frame against the approved anchor.

    What fails (see ``failures``): parts the candidate INVENTED (no counterpart anywhere on the
    anchor's design), parts that should be visible from the candidate's facing but are missing,
    a different character, or a camera elevation more than ~15 degrees off. Parts hidden because
    the character turned (``expected_hidden_parts``) are informational and never fail.
    """
    status: str  # match | mismatch | unavailable | error
    model_used: str = ""
    anchor_facing: str | None = None
    candidate_facing: str | None = None
    invented_parts: list[PartNote] = Field(default_factory=list)
    missing_visible_parts: list[PartNote] = Field(default_factory=list)
    changed_parts: list[PartNote] = Field(default_factory=list)
    expected_hidden_parts: list[PartNote] = Field(default_factory=list)
    tool_sides: dict[str, Any] = Field(default_factory=dict)
    anchor_elevation: int | None = None
    candidate_elevation: int | None = None
    # Legacy fields (the first comparison prompt asked for a flat list of differences and hands).
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

    def elevation_gap(self) -> int | None:
        a = self.anchor_elevation if self.anchor_elevation is not None else self.reference_camera_elevation_degrees
        c = self.candidate_elevation if self.candidate_elevation is not None else self.candidate_camera_elevation_degrees
        if a is None or c is None:
            return None
        return abs(int(c) - int(a))

    def failures(self) -> list[str]:
        """The verdict rule: every reason this candidate does not match the anchor (empty = match)."""
        out = [f"invented: {p.text()} -- no counterpart on the anchor's design" for p in self.invented_parts]
        where = f" from {self.candidate_facing}" if self.candidate_facing else ""
        out += [f"missing: {p.text()} -- should be visible{where}" for p in self.missing_visible_parts]
        if self.same_character is False:
            out.append("identity: the model does not read the candidate as the same character")
        gap = self.elevation_gap()
        if gap is not None and gap > ELEVATION_TOLERANCE_DEGREES:
            a = self.anchor_elevation if self.anchor_elevation is not None else self.reference_camera_elevation_degrees
            c = self.candidate_elevation if self.candidate_elevation is not None else self.candidate_camera_elevation_degrees
            out.append(f"perspective: candidate camera {c} deg vs anchor {a} deg "
                       f"(more than {ELEVATION_TOLERANCE_DEGREES} deg apart)")
        return out


def _part_notes(raw: Any) -> list[PartNote]:
    notes: list[PartNote] = []
    for item in raw or []:
        if isinstance(item, dict):
            part = str(item.get("part") or item.get("name") or item.get("element") or "").strip()
            note = PartNote(
                part=part,
                location=str(item.get("location") or item.get("where") or "").strip(),
                size=str(item.get("size") or item.get("size_estimate") or "").strip(),
                detail=str(item.get("detail") or item.get("change") or item.get("reason") or item.get("why") or "").strip(),
            )
        elif isinstance(item, str) and item.strip():
            note = PartNote(part=item.strip())
        else:
            continue
        if note.part or note.detail:
            notes.append(note)
    return notes


def _degrees(value: Any) -> int | None:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def parse_reference_response(
    parsed: dict[str, Any],
    *,
    model: str = "",
    cost: float | None = None,
    facing: str | None = None,
    anchor_facing: str | None = "south",
) -> ReferenceComparison:
    """Turn the vision model's JSON into a comparison and apply the verdict rule.

    The model's own "verdict" is not trusted: status is ``match`` only when ``failures()``
    is empty. A "missing" part whose explanation is the turn itself ("absent as it is turned
    away") is moved to ``expected_hidden_parts``: occlusion is expected, never a defect.
    """
    missing: list[PartNote] = []
    hidden = _part_notes(parsed.get("expected_hidden_parts"))
    for note in _part_notes(parsed.get("missing_visible_parts")):
        if _OCCLUSION_RE.search(f"{note.part} {note.detail} {note.location}"):
            hidden.append(note)
        else:
            missing.append(note)
    sides = parsed.get("tool_sides")
    same = parsed.get("same_character")
    comparison = ReferenceComparison(
        status="match",
        model_used=model,
        anchor_facing=anchor_facing,
        candidate_facing=facing,
        invented_parts=_part_notes(parsed.get("invented_parts")),
        missing_visible_parts=missing,
        changed_parts=_part_notes(parsed.get("changed_parts")),
        expected_hidden_parts=hidden,
        tool_sides=sides if isinstance(sides, dict) else {},
        anchor_elevation=_degrees(parsed.get("anchor_elevation")),
        candidate_elevation=_degrees(parsed.get("candidate_elevation")),
        same_character=same if isinstance(same, bool) else None,
        notes=str(parsed.get("notes", "")),
        cost_usd=cost,
    )
    comparison.reference_camera_elevation_degrees = comparison.anchor_elevation
    comparison.candidate_camera_elevation_degrees = comparison.candidate_elevation
    gap = comparison.elevation_gap()
    comparison.perspective_match = None if gap is None else gap <= ELEVATION_TOLERANCE_DEGREES
    failures = comparison.failures()
    comparison.status = "mismatch" if failures else "match"
    comparison.verdict = comparison.status
    return comparison


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


def reference_prompt(
    *,
    candidate_facing: str,
    anchor_facing: str = "south",
    description: str = "",
    expected_perspective: str = "low top-down (approx 20-30 degrees)",
) -> str:
    """The comparison question. Primary: what did the candidate INVENT? Occlusion is not a difference."""
    turned = candidate_facing != anchor_facing
    turn_text = (
        f"The ANCHOR shows the character facing {anchor_facing}. The CANDIDATE should be the SAME character turned to "
        f"face {candidate_facing}. Because it turned, some faces of the body now point away from the camera: parts on "
        f"those faces (for a back view: the face, visor, chest details, anything on the front) are EXPECTED to be "
        f"hidden. Do NOT report them as missing or removed; list them only under expected_hidden_parts."
        if turned else
        f"Both images should show the same character facing {anchor_facing} from the same camera."
    )
    return f"""You are a pixel-art art director checking a sprite against its approved design, on one board.
LEFT = ANCHOR (the approved design). RIGHT = CANDIDATE.
Subject: {description or 'game character'}. Expected camera: {expected_perspective}.
{turn_text}

Answer these questions in order.

1. INVENTED PARTS (the main question). Look at every element drawn in the CANDIDATE: bolts, bosses, rivets,
   lights, eyes, handles, antennae, vents, panels, logos, extra limbs, extra wheels, orbs, floating fragments.
   For each one ask: does this element have a counterpart ANYWHERE on the anchor's design (also on a side the
   anchor does not show but clearly implies)? List every element that does NOT: the generator made it up.
   Give its location on the sprite and a size estimate. A round boss or bolt on the back or the sides of the
   head that the anchor's head does not have is an invented part.
2. MISSING PARTS. List parts of the anchor's design that SHOULD be visible from the candidate's facing
   ({candidate_facing}) but are not drawn. Never list a part here because the turn hides it.
3. CHANGED PARTS. Parts present in both whose size, colour or material changed.
4. EXPECTED HIDDEN PARTS. Parts of the anchor that the candidate's facing legitimately hides (information only).
5. HELD TOOLS. For each held tool or attachment: which of the CHARACTER'S OWN sides (left/right) it is on, in
   the anchor and in the candidate.
6. CAMERA. The camera elevation of each image in degrees (0 = side view, 90 = straight down).
7. Is it the same character?

Return a single JSON object with exactly these keys:
{{
  "invented_parts": [{{"part": string, "location": string, "size": string}}],
  "missing_visible_parts": [{{"part": string, "location": string, "detail": string}}],
  "changed_parts": [{{"part": string, "detail": string}}],
  "expected_hidden_parts": [string],
  "tool_sides": {{"anchor": {{"<tool>": "left|right|none"}}, "candidate": {{"<tool>": "left|right|none"}}}},
  "anchor_elevation": integer,
  "candidate_elevation": integer,
  "same_character": boolean,
  "notes": string (1-2 sentences)
}}
Empty lists are the right answer when nothing applies. Only return valid JSON."""


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
    # The verdict rule: invented parts, parts missing though they should be visible, another
    # character, or a camera more than ~15 degrees off. Occlusion by the turn never fails.
    errors: list[str] = list(comparison.failures())
    for d in comparison.anomalies():  # legacy flat differences
        errors.append(f"{d.kind}: {d.part} {('- ' + d.detail) if d.detail else ''}".strip())
    if comparison.perspective_match is False and not any(e.startswith("perspective:") for e in errors):
        errors.append(
            f"perspective: candidate camera {comparison.candidate_camera_elevation_degrees} deg vs reference "
            f"{comparison.reference_camera_elevation_degrees} deg"
        )
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
    warnings = [f"{d.kind}: {d.part} {d.detail}".strip() for d in other]
    warnings += [f"changed: {p.text()}" for p in comparison.changed_parts]
    sides = comparison.tool_sides
    if isinstance(sides.get("anchor"), dict) and isinstance(sides.get("candidate"), dict):
        for tool, side in sides["anchor"].items():
            other_side = sides["candidate"].get(tool)
            if side and other_side and str(side).strip().lower() != str(other_side).strip().lower():
                warnings.append(f"tool side: {tool} reads as {other_side} in the candidate, {side} on the anchor "
                                f"(check with judge-views' marker-based tool-side check)")
    hidden = [p.text() for p in comparison.expected_hidden_parts]
    return GateResult(
        name="vision_reference",
        passed=passed,
        score=1.0 if passed else max(0.0, 1.0 - 0.25 * len(errors)),
        summary=(f"Matches the reference{where}: {comparison.notes or comparison.verdict}" if passed
                 else f"Differs from the reference{where}: {errors[0]}"),
        errors=errors,
        warnings=warnings,
        metrics={
            "status": comparison.status,
            "model": comparison.model_used,
            "invented_parts": len(comparison.invented_parts),
            "missing_visible_parts": len(comparison.missing_visible_parts),
            "changed_parts": len(comparison.changed_parts),
            "expected_hidden_parts": hidden,
            "tool_sides": comparison.tool_sides,
            "anchor_elevation": comparison.anchor_elevation,
            "candidate_elevation": comparison.candidate_elevation,
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
        anchor_facing: str = "south",
        description: str = "",
        expected_perspective: str = "low top-down (approx 20-30 degrees)",
        zoom: int = 6,
    ) -> ReferenceComparison:
        """Anchor + candidate on one board; ask what the candidate INVENTED and what it lost.

        ``facing`` is the candidate's facing (default: the anchor's), ``anchor_facing`` the
        anchor's. The model is told that parts on faces turned away from the camera are
        expected to be hidden; the verdict comes from ``parse_reference_response`` (invented
        parts, missing-but-should-be-visible parts, another character, elevation > ~15 deg).
        Offline (no key) returns status ``unavailable``: the caller's gate must fail, never accept.
        """
        if not self.is_available():
            return ReferenceComparison(
                status="unavailable",
                model_used="offline_stub",
                notes="No vision model available (OPENROUTER_API_KEY not configured or SPRITEWARDEN_OFFLINE set)",
            )

        candidate_facing = facing or anchor_facing
        prompt = reference_prompt(
            candidate_facing=candidate_facing,
            anchor_facing=anchor_facing,
            description=description,
            expected_perspective=expected_perspective,
        )
        board = prepare_pair_for_vision(reference, candidate, zoom=zoom, labels=("ANCHOR", "CANDIDATE"))
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": _to_data_url(board)}},
        ]
        parsed, cost, err = self._chat_json(content)
        if err or not isinstance(parsed, dict):
            return ReferenceComparison(status="error", model_used=self.vision_model, notes=err or "unparseable response")

        return parse_reference_response(
            parsed, model=self.vision_model, cost=cost, facing=candidate_facing, anchor_facing=anchor_facing
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

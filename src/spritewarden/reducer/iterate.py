"""AI-driven refinement loop: render candidate reductions, judge them with a vision
model, map its critique onto pipeline parameters, and iterate until convergence.

Falls back to the deterministic offline objective (metrics.deterministic_score)
when OPENROUTER_API_KEY is unavailable.
"""

from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field

from ..judge import evaluate_still, VisionJudge
from ..judge.vision import DEFAULT_VISION_MODEL
from ..router.jev import _get_openrouter_key
from .core import ReduceParams, reduce_image, AUTO_WIDTH_LADDER
from .metrics import evaluate_candidate

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
VISION_WEIGHT = 0.6
DET_WEIGHT = 0.4
CONVERGENCE_EPS = 0.02
SHEET_ITEM_HEIGHT = 360
SHEET_MAX_ITEM_WIDTH = 800

_PARAM_CLAMPS = {
    "brightness": (0.5, 1.6),
    "contrast": (0.5, 1.6),
    "saturation": (0.5, 1.6),
    "pre_blur": (0.0, 4.0),
    "dither_strength": (0.0, 1.5),
    "width": (8, 512),
}


class VisionCandidateVerdict(BaseModel):
    """One candidate's critique from the vision model."""

    id: str
    fidelity: float = 0.0
    cleanliness: float = 0.0
    harmony: float = 0.0
    artifacts: list[str] = Field(default_factory=list)
    notes: str = ""

    @property
    def overall(self) -> float:
        return (self.fidelity + self.cleanliness + self.harmony) / 30.0


class VisionCritique(BaseModel):
    """Full set-level response from the vision model."""

    candidates: dict[str, VisionCandidateVerdict]
    best: str | None = None
    suggestions: list[str] = Field(default_factory=list)


class CandidateScore(BaseModel):
    """A rendered candidate with all scoring attached."""

    id: str
    params: ReduceParams
    image_path: str = ""
    det_score: float = 0.0
    det_metrics: dict[str, Any] = Field(default_factory=dict)
    vision: VisionCandidateVerdict | None = None
    total: float = 0.0

    @property
    def vision_overall(self) -> float:
        return self.vision.overall if self.vision else 0.0


class RoundRecord(BaseModel):
    """One refinement round."""

    index: int
    candidates: list[CandidateScore]
    contact_sheet: str = ""
    best_id: str = ""
    suggestions_applied: list[str] = Field(default_factory=list)


class TuningReport(BaseModel):
    """Final output of the refinement loop."""

    source: str
    palette: str
    used_vision: bool
    converged: bool
    rounds: list[RoundRecord]
    best: CandidateScore
    best_image: str
    preview_image: str
    final_verdict: dict[str, Any] | None = None
    total_cost_usd: float | None = None


# ---------------------------------------------------------------------------
# Candidate generation
# ---------------------------------------------------------------------------


def _ladder_step(width: int, direction: int) -> int:
    if direction < 0:
        below = [w for w in AUTO_WIDTH_LADDER if w < width]
        return below[-1] if below else max(8, int(width * 0.75))
    above = [w for w in AUTO_WIDTH_LADDER if w > width]
    return above[0] if above else min(512, int(width * 1.5))


def _dedupe(cands: list[ReduceParams]) -> list[ReduceParams]:
    seen: set[str] = set()
    out = []
    for c in cands:
        key = c.model_dump_json()
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def initial_candidates(source_size: tuple[int, int], base: ReduceParams) -> list[ReduceParams]:
    """Round 0 grid: diverse downscale resolutions and dither strategies."""
    if base.width is not None or base.height is not None:
        return _dedupe([
            base,
            base.model_copy(update={"dither": "bayer4", "dither_strength": 0.8}),
            base.model_copy(update={"dither": "floyd", "dither_strength": 0.7}),
            base.model_copy(update={"downscale": "dominant"}),
        ])
    mid = min(AUTO_WIDTH_LADDER, key=lambda w: abs(w - 64))
    below = _ladder_step(mid, -1)
    above = _ladder_step(mid, +1)
    return _dedupe([
        base.model_copy(update={"width": mid}),
        base.model_copy(update={"width": mid, "dither": "bayer4", "dither_strength": 0.8}),
        base.model_copy(update={"width": below}),
        base.model_copy(update={"width": above, "dither": "floyd", "dither_strength": 0.7}),
    ])


_SUGGESTION_RULES: list[tuple[str, str, dict[str, Any]]] = [
    (r"too dark|darker|underexposed|murky", "brighten", {"brightness": 0.08}),
    (r"too (bright|light)|overexposed|blown out|washed out by light", "darken", {"brightness": -0.08}),
    (r"washed out|flat|low contrast|pale", "raise contrast", {"contrast": 0.1}),
    (r"harsh|too much contrast|crushed blacks", "lower contrast", {"contrast": -0.1}),
    (r"desaturated|dull|drab|grayish|greyish|monotone|muddy colors", "raise saturation", {"saturation": 0.12}),
    (r"too saturated|neon|garish|over-saturated", "lower saturation", {"saturation": -0.12}),
    (r"noisy|grainy|busy|speckled|dither noise|static", "reduce dither", {"dither_strength": -0.4}),
    (r"banding|posteriz|stepping", "add ordered dither", {"dither": "bayer4", "dither_strength": 0.3}),
    (r"unclear|mushy|blurry|smudged|indistinct|soft shapes", "sharpen (less blur, finer grid)", {"pre_blur": -0.25, "width_up": 1}),
    (r"aliased|jagged|shimmer|crawl", "soften (more pre-blur)", {"pre_blur": 0.25}),
    (r"blocky|chunky|coarse|too low res", "raise resolution", {"width_up": 1}),
    (r"too fine|too small|lost detail", "lower resolution", {"width_up": -1}),
]


def _clamp_param(params: ReduceParams) -> ReduceParams:
    for key, (lo, hi) in _PARAM_CLAMPS.items():
        val = getattr(params, key)
        if val is None:
            continue
        setattr(params, key, min(hi, max(lo, val)))
    if params.width is not None:
        params.width = int(params.width)
    return params


def apply_suggestions(params: ReduceParams, suggestions: list[str]) -> tuple[ReduceParams, list[str]]:
    """Map vision critique phrases onto parameter deltas. Returns (params, applied rule labels)."""
    applied: list[str] = []
    text = " | ".join(s for s in suggestions if s).lower()
    for pattern, label, deltas in _SUGGESTION_RULES:
        if not re.search(pattern, text):
            continue
        if len(applied) >= 3:
            break
        applied.append(label)
        for key, delta in deltas.items():
            if key == "width_up":
                if params.width is not None:
                    params.width = _ladder_step(params.width, int(delta))
                continue
            setattr(params, key, getattr(params, key) + delta)
    return _clamp_param(params), applied


def neighbor_candidates(best: ReduceParams, round_index: int) -> list[ReduceParams]:
    """Coordinate descent: vary one axis at a time around the current best."""
    axes: list[ReduceParams] = []
    if best.width is not None:
        axes.append(best.model_copy(update={"width": _ladder_step(best.width, -1)}))
        axes.append(best.model_copy(update={"width": _ladder_step(best.width, +1)}))
    if best.dither != "none":
        axes.append(best.model_copy(update={"dither_strength": max(0.0, best.dither_strength - 0.35)}))
        axes.append(best.model_copy(update={"dither_strength": min(1.5, best.dither_strength + 0.35)}))
    else:
        axes.append(best.model_copy(update={"dither": "bayer4", "dither_strength": 0.6}))
    tone_axis = ["saturation", "brightness", "contrast"][round_index % 3]
    delta = 0.1 if tone_axis in ("saturation", "contrast") else 0.08
    tone = {tone_axis: max(_PARAM_CLAMPS[tone_axis][0], min(_PARAM_CLAMPS[tone_axis][1], getattr(best, tone_axis) + delta))}
    axes.append(best.model_copy(update=tone))
    return _dedupe(axes)[:4]


# ---------------------------------------------------------------------------
# Contact sheet + vision scoring
# ---------------------------------------------------------------------------


def _fit_item(img: Image.Image) -> Image.Image:
    h, w = img.height, img.width
    scale = max(1, min(SHEET_ITEM_HEIGHT // max(1, h), SHEET_MAX_ITEM_WIDTH // max(1, w)))
    if scale > 1:
        img = img.resize((w * scale, h * scale), Image.NEAREST)
    if img.height > SHEET_ITEM_HEIGHT:
        img = img.resize((img.width * SHEET_ITEM_HEIGHT // img.height, SHEET_ITEM_HEIGHT), Image.Resampling.LANCZOS)
    return img


def build_contact_sheet(
    source: Image.Image,
    candidates: list[tuple[str, Image.Image]],
) -> Image.Image:
    """Horizontal board: SRC panel then labeled candidate panels."""
    src = _fit_item(source)
    items = [("SRC", src)] + [(cid, _fit_item(img)) for cid, img in candidates]

    LABEL_H = 64
    GUTTER = 24
    BOARD = (128, 128, 128)
    label_font = ImageFont.load_default(size=40)

    total_w = sum(im.width for _, im in items) + GUTTER * (len(items) + 1)
    total_h = SHEET_ITEM_HEIGHT + LABEL_H + GUTTER * 2
    board = Image.new("RGB", (total_w, total_h), BOARD)
    draw = ImageDraw.Draw(board)
    x = GUTTER
    for label, im in items:
        strip = Image.new("RGB", (im.width, LABEL_H), (220, 220, 220))
        sdraw = ImageDraw.Draw(strip)
        color = (20, 20, 20) if label == "SRC" else (0, 0, 0)
        sdraw.text((8, 8), label, fill=color, font=label_font)
        board.paste(strip, (x, GUTTER))
        flat = im.convert("RGBA")
        bg = Image.new("RGBA", flat.size, BOARD + (255,))
        board.paste(Image.alpha_composite(bg, flat).convert("RGB"), (x, GUTTER + LABEL_H))
        x += im.width + GUTTER
    return board


def _to_data_url(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _parse_json_loose(text: str) -> dict[str, Any] | None:
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return None


def score_with_vision(
    source: Image.Image,
    candidates: list[tuple[str, Image.Image]],
    description: str,
    vision_model: str = DEFAULT_VISION_MODEL,
    api_key: str | None = None,
) -> VisionCritique | None:
    """One vision call over the whole contact sheet. Returns None when unavailable or failed."""
    key = api_key or _get_openrouter_key()
    if not key:
        return None
    sheet = build_contact_sheet(source, candidates)
    ids = [cid for cid, _ in candidates]
    prompt = f"""You are a strict pixel-art conversion critic. The "SRC" panel is the original artwork.
Panels {', '.join(ids)} are that artwork reduced to a small fixed pixel palette.

Score every panel 0-10 (integers) on:
- fidelity: subject, shapes, and overall colors preserved compared to SRC
- cleanliness: no mush, dither noise, mixel artifacts, or banding
- harmony: the limited palette is used well with good tonal range

Also list concrete artifacts per panel and 1-3 improvement directions for the whole set,
phrased like: "too dark", "too bright", "washed out", "harsh", "desaturated", "too saturated",
"noisy", "banding", "unclear shapes", "aliased", "blocky", "too fine".
Subject context: {description or 'general artwork conversion'}.

Return ONLY JSON:
{{"candidates":[{{"id":"A","fidelity":7,"cleanliness":8,"harmony":7,"artifacts":["..."],"notes":"..."}}],
"best":"A","suggestions":["..."]}}"""
    payload = {
        "model": vision_model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": _to_data_url(sheet)}},
            ],
        }],
        "response_format": {"type": "json_object"},
    }
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                OPENROUTER_CHAT_URL,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            raw = data["choices"][0]["message"]["content"]
            parsed = _parse_json_loose(raw)
            if not parsed:
                return None
            out: dict[str, VisionCandidateVerdict] = {}
            for entry in parsed.get("candidates", []):
                if not isinstance(entry, dict) or "id" not in entry:
                    continue
                out[str(entry["id"])] = VisionCandidateVerdict(
                    id=str(entry["id"]),
                    fidelity=float(entry.get("fidelity", 0) or 0),
                    cleanliness=float(entry.get("cleanliness", 0) or 0),
                    harmony=float(entry.get("harmony", 0) or 0),
                    artifacts=[str(a) for a in entry.get("artifacts", [])],
                    notes=str(entry.get("notes", "")),
                )
            if not out:
                return None
            best_val = parsed.get("best")
            return VisionCritique(
                candidates=out,
                best=str(best_val) if best_val else None,
                suggestions=[str(s) for s in parsed.get("suggestions", []) if s],
            )
    except Exception:
        return None


def extract_suggestions(critique: VisionCritique | None) -> list[str]:
    """Set-level suggestions plus the winning candidate's artifacts + notes."""
    if not critique:
        return []
    phrases = list(critique.suggestions)
    focus = critique.candidates.get(critique.best) if critique.best else None
    for v in ([focus] if focus else list(critique.candidates.values())):
        phrases.extend(v.artifacts)
        if v.notes:
            phrases.append(v.notes)
    return phrases


# ---------------------------------------------------------------------------
# Main refinement loop
# ---------------------------------------------------------------------------


def refine(
    image_or_path: str | Path | Image.Image,
    palette_source: str | Path | list[str] | list[tuple[int, int, int]] | None = None,
    params: ReduceParams | None = None,
    out_dir: str | Path | None = None,
    max_rounds: int = 4,
    use_vision: bool = True,
    vision_model: str = DEFAULT_VISION_MODEL,
    description: str = "",
    api_key: str | None = None,
) -> TuningReport:
    """Iteratively render candidates, score them (vision + deterministic), and converge.

    Each round renders up to 4 candidates, sends one contact-sheet vision call
    (when use_vision and an API key are available), applies the critique as
    parameter deltas, and repeats until improvement falls below CONVERGENCE_EPS
    or max_rounds. Pass api_key="" to force deterministic-only scoring.
    """
    base = params or ReduceParams()
    if isinstance(image_or_path, (str, Path)):
        source = Image.open(image_or_path).convert("RGBA")
        source_label = str(image_or_path)
    else:
        source = image_or_path.convert("RGBA")
        source_label = "<PIL.Image>"

    from ..judge.palette import load_palette, rgb_to_hex

    palette = load_palette(palette_source)
    palette_rgb = np.array(palette, dtype=np.uint8)
    palette_hex = [rgb_to_hex(tuple(c)) for c in palette]

    session = Path(out_dir) if out_dir else (
        Path(source_label).with_name(Path(source_label).stem + "_pixelize")
        if source_label != "<PIL.Image>" else Path.cwd() / "pixelize_session"
    )
    session.mkdir(parents=True, exist_ok=True)
    rounds_dir = session / "rounds"
    rounds_dir.mkdir(exist_ok=True)

    judge: VisionJudge | None = None
    if use_vision:
        if api_key == "":
            judge = None  # explicit empty key -> force deterministic mode
        else:
            judge = VisionJudge(vision_model=vision_model, api_key=api_key)
    if judge and not judge.is_available():
        judge = None

    candidates_params = initial_candidates(source.size, base)
    ids = "ABCDEFGH"
    best: CandidateScore | None = None
    records: list[RoundRecord] = []
    converged = False

    for round_i in range(max_rounds):
        trimmed = candidates_params[: len(ids)]
        rendered: list[tuple[str, Image.Image]] = []
        scores: list[CandidateScore] = []
        for i, p in enumerate(trimmed):
            img = reduce_image(source, p, palette_rgb)
            cid = ids[i]
            path = rounds_dir / f"round_{round_i}" / f"{cid}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            img.save(path)
            rendered.append((cid, img))
            det_score, det_metrics = evaluate_candidate(img, source, palette_hex)
            scores.append(CandidateScore(
                id=cid,
                params=p,
                image_path=str(path),
                det_score=det_score,
                det_metrics=det_metrics,
            ))

        vision_critique = None
        if judge:
            vision_critique = score_with_vision(
                source=source,
                candidates=rendered,
                description=description,
                vision_model=vision_model,
            )
        for s in scores:
            if vision_critique and s.id in vision_critique.candidates:
                s.vision = vision_critique.candidates[s.id]
            s.total = round(
                VISION_WEIGHT * s.vision_overall + DET_WEIGHT * s.det_score if s.vision else s.det_score,
                4,
            )

        best_of_round = max(scores, key=lambda s: s.total)
        sheet_path = ""
        if judge or round_i == 0:
            sheet = build_contact_sheet(source, rendered)
            sheet_path = str(session / f"contact_round_{round_i}.png")
            sheet.save(sheet_path)

        if best is None:
            best = best_of_round.model_copy()
        else:
            gain = best_of_round.total - best.total
            if gain > 0:
                best = best_of_round.model_copy()
            if gain < CONVERGENCE_EPS:
                converged = True

        round_best_params = best_of_round.params.model_copy()
        suggestions = extract_suggestions(vision_critique)
        adjusted, applied = apply_suggestions(round_best_params, suggestions)
        if applied:
            candidates_params = neighbor_candidates(adjusted, round_i + 1)
        else:
            candidates_params = neighbor_candidates(round_best_params, round_i + 1)
        records.append(RoundRecord(
            index=round_i,
            candidates=scores,
            contact_sheet=sheet_path,
            best_id=best_of_round.id,
            suggestions_applied=applied,
        ))
        if converged:
            break

    assert best is not None
    final_path = session / "final.png"
    final_img = reduce_image(source, best.params, palette_rgb)
    final_img.save(final_path)
    preview_scale = max(2, min(16, 512 // max(1, max(final_img.size))))
    preview_path = session / "final_preview.png"
    final_img.resize(
        (final_img.width * preview_scale, final_img.height * preview_scale),
        Image.NEAREST,
    ).save(preview_path)

    best.image_path = str(final_path)
    best.det_metrics = evaluate_candidate(final_img, source, palette_hex)[1]

    final_verdict = None
    if judge:
        card = evaluate_still(final_img, palette_source=palette_source)
        verdict = judge.orchestrate_judgment(
            still_scorecard=card,
            image=final_img,
            prompt_description=description or "palette-constrained pixel art conversion",
            expected_perspective="n/a (converted artwork)",
        )
        final_verdict = verdict.model_dump()

    report = TuningReport(
        source=source_label,
        palette=str(palette_source),
        used_vision=judge is not None,
        converged=converged,
        rounds=records,
        best=best,
        best_image=str(final_path),
        preview_image=str(preview_path),
        final_verdict=final_verdict,
        total_cost_usd=None,
    )
    (session / "report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return report

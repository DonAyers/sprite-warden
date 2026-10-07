"""The enforcement core: map a tool call to (provider, operation), run the recipe's rules.

Ported from the game repo's ``tools/generator_hook.py`` (2026-10-06: "ENFORCE these
flows"), with the hardcoded rules replaced by the registry's ``enforce`` blocks:

provider level (``providers.<name>.enforce``)::

    {"mcp_prefixes": ["mcp__pixellab__"],            # MCP tool names that belong to the provider
     "read_only": ["^get_", ...],                    # short tool names that never spend (ignored)
     "spends": ["^generate_", ...],                  # if present: ONLY these short names spend
     "operation_overrides": [{"tool", "arg_present", "operation"}],
     "cli": {"spending_ops": [...], "operation_from": [{"arg", "prefix", "operation", "strip_prefix"}]},
     "rules": [...],                                 # apply to every spending call of the provider
     "source": [{"kind": "url", "args": [...]}]}     # source-image resolution for every call

operation level (``operations.<name>.enforce``)::

    {"stage": "rotation" | "animation",              # checked against the nearest pipeline.json
     "source": [{"kind": "path" | "manifest_asset" | "url" | "pixellab_character", "args": [...]}],
     "rules": [{"kind": <one of RULE_KINDS>, ...}]}

A call with a resolved source image must find a current passed gate record beside
it (``gates/records.py``); the accepted gate names are the recipe's
``required_gates_before`` plus the umbrella names.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import ProjectConfig, load_config

RULE_KINDS = (
    "max_items",
    "forbid_args",
    "enum",
    "multiple_of",
    "forbid_inline_image_args",
    "forbidden_words",
    "no_dimensions_in_prompt",
    "loop_engine",
    "require_arg_value",
    "matte_collision",
)
UMBRELLA_GATES = ("still", "start_image", "views", "anim")
GATE_ALIASES = {"direction_set": "views", "views": "direction_set"}
VIEW_GATES = ("views", "direction_set")
REVIEW_GATES = ("still", "start_image", "views", "anim")
PROMPT_ARGS = ("prompt", "description", "action_description")
PROVIDER_LABELS = {
    "pixellab": "PixelLab",
    "spritefusion": "Sprite Fusion",
    "retrodiffusion": "Retro Diffusion",
    "spritecook": "SpriteCook",
    "openrouter_image": "OpenRouter image",
}
WARDEN_COMMANDS = ("warden", "sprite-warden", "spritewarden")
WARDEN_MODULES = ("spritewarden.cli", "spritewarden")
CONTEXT_LIMIT = 2400

# Never a spending call, whatever the provider (the spec's read-only list).
_READ_ONLY = re.compile(
    r"^(get_|list_|check_|wait_for_jobs$|update_asset_label$|create_asset_upload$|finalize_asset_upload$|"
    r"import_asset$|uploads?$|finalize$|balance$|status$|credits$|styles$|models$|help$|--help$|-h$)"
)
_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'|[;|&]+|[^\s;|&]+')
_ASSET_URL = re.compile(r"/v1/assets/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:[/?#]|$)")
_DIMENSIONS = (re.compile(r"\b\d{2,3}\s*[x×]\s*\d{2,3}\b", re.I), re.compile(r"\b\d{2,3}\s*px\b", re.I))
# Stems that name a facing (kept equal to judge.views.FACING_ALIASES by a test; that module needs PIL).
FACING_STEMS = frozenset({
    "s", "down", "front", "south", "se", "down_right", "southeast", "south_east", "e", "right", "east",
    "ne", "up_right", "northeast", "north_east", "n", "up", "back", "north", "nw", "up_left", "northwest",
    "north_west", "w", "left", "west", "sw", "down_left", "southwest", "south_west",
})


@dataclass
class Verdict:
    """The answer for one tool call.

    ``decision`` is ``allow`` | ``deny`` | ``ignore`` (``ignore`` = not a spending generator
    call). ``reasons`` lists the broken rules (also on an overridden ``allow``); ``context`` is
    the text a hook injects on ``allow`` (recipe rules, an override notice or a gatekeeper error).
    """

    decision: str
    provider: str | None = None
    operation: str | None = None
    reasons: list[str] = field(default_factory=list)
    context: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Call:
    provider: str
    operation: str
    tool: str
    via: str  # mcp | cli | warden
    args: dict[str, Any]
    prov: dict[str, Any]
    entry: dict[str, Any] | None


@dataclass
class _Source:
    path: Path
    kind: str  # file | views_dir
    label: str


# ----------------------------------------------------------------------------- registry (raw JSON)

_REGISTRY_CACHE: dict[str, dict[str, Any]] = {}


def default_registry_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "recipes.json"


def load_raw_registry(path: str | Path | None = None) -> dict[str, Any]:
    """The registry as plain JSON (the pydantic models in router/recipes.py are too slow for a hook)."""
    p = Path(path) if path else default_registry_path()
    key = str(p)
    if key not in _REGISTRY_CACHE:
        _REGISTRY_CACHE[key] = json.loads(p.read_text(encoding="utf-8"))
    return _REGISTRY_CACHE[key]


def _norm(name: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def _canonical_provider(reg: dict[str, Any], name: Any) -> str | None:
    key = _norm(name)
    if not key:
        return None
    for pname, prov in (reg.get("providers") or {}).items():
        if _norm(pname) == key or key in {_norm(a) for a in prov.get("aliases", [])}:
            return pname
    return None


def _find_operation(prov: dict[str, Any], operation: Any) -> tuple[str | None, dict[str, Any] | None]:
    key = _norm(operation)
    if not key:
        return None, None
    for oname, entry in (prov.get("operations") or {}).items():
        if _norm(oname) == key or key in {_norm(a) for a in entry.get("aliases", [])}:
            return oname, entry
    return None, None


def _enforce(block: dict[str, Any] | None) -> dict[str, Any]:
    return (block or {}).get("enforce") or {}


# ----------------------------------------------------------------------------- classification

def _tokens(command: str) -> list[str]:
    out = []
    for tok in _TOKEN.findall(command):
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
            tok = tok[1:-1]
        out.append(tok)
    return out


def _is_separator(tok: str) -> bool:
    return bool(tok) and all(c in ";|&" for c in tok)


def _parse_flags(tokens: list[str]) -> dict[str, Any]:
    """``--flag value`` pairs as a dict (dashes -> underscores; repeats and multi-values become lists)."""
    args: dict[str, Any] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if not tok.startswith("--") or len(tok) < 3:
            continue
        key, eq, inline = tok[2:].partition("=")
        key = key.replace("-", "_")
        values: list[str] = [inline] if eq else []
        if not eq:
            while i < len(tokens) and not tokens[i].startswith("--"):
                values.append(tokens[i])
                i += 1
        value: Any = True if not values else (values[0] if len(values) == 1 else values)
        if key in args:
            old = args[key] if isinstance(args[key], list) else [args[key]]
            new = value if isinstance(value, list) else [value]
            args[key] = [v for v in old + new if v is not True] or True
        else:
            args[key] = value
    return args


def _segment(tokens: list[str], start: int) -> list[str]:
    """The tokens of one command, from ``start`` up to the next shell separator."""
    out = []
    for tok in tokens[start:]:
        if _is_separator(tok):
            break
        out.append(tok)
    return out


def _classify_command(command: str, cfg: ProjectConfig, get_reg: Callable[[], dict[str, Any]]) -> _Call | None:
    tokens = _tokens(command)
    if not tokens:
        return None
    for i, tok in enumerate(tokens):
        norm = tok.replace("\\", "/")
        base = norm.rsplit("/", 1)[-1].lower()
        if base.endswith(".exe"):
            base = base[:-4]
        # a project CLI listed in the config: python tools/sprite_fusion.py <op> ...
        for script, provider in cfg.cli_tools.items():
            if norm == script or norm.endswith("/" + script):
                rest = _segment(tokens, i + 1)
                return _cli_call(provider, rest, get_reg())
        # warden generate / python -m spritewarden.cli generate
        is_warden = base in WARDEN_COMMANDS and (i == 0 or _is_separator(tokens[i - 1]) or "/" in norm)
        is_module = tok in WARDEN_MODULES and i > 0 and tokens[i - 1] == "-m"
        if is_warden or is_module:
            rest = _segment(tokens, i + 1)
            if rest and rest[0] == "generate":
                return _warden_call(rest[1:], get_reg())
    return None


def _cli_call(provider: str, rest: list[str], reg: dict[str, Any]) -> _Call | None:
    op = next((t for t in rest if not t.startswith("-")), None)
    if op is None or _READ_ONLY.search(op.lower()):
        return None
    canon = _canonical_provider(reg, provider)
    prov = (reg.get("providers") or {}).get(canon, {}) if canon else {}
    cli = _enforce(prov).get("cli") or {}
    name, entry = _find_operation(prov, op)
    spending = cli.get("spending_ops")
    if spending is not None and op not in spending and entry is None:
        return None
    args = _parse_flags(rest[rest.index(op) + 1:])
    for rule in cli.get("operation_from", []):
        value = args.get(rule.get("arg", ""))
        if not isinstance(value, str) or not value:
            continue
        prefix = rule.get("prefix")
        if prefix is None:
            n2, e2 = _find_operation(prov, value)
            if e2 is not None:
                name, entry, op = n2, e2, value
                break
            continue
        if value.startswith(prefix):
            target = rule.get("operation") or (value[len(prefix):] if rule.get("strip_prefix") else value)
            name, entry = _find_operation(prov, target)
            op = target
            break
    return _Call(provider=canon or provider, operation=name or op, tool=op, via="cli", args=args, prov=prov, entry=entry)


def _warden_call(rest: list[str], reg: dict[str, Any]) -> _Call:
    args = _parse_flags(rest)
    prompt = next((t for t in rest if not t.startswith("-")), None)
    if prompt and "prompt" not in args:
        args["prompt"] = prompt
    given = args.get("provider") if isinstance(args.get("provider"), str) else None
    canon = _canonical_provider(reg, given) if given else None
    prov = (reg.get("providers") or {}).get(canon, {}) if canon else {}
    name, entry = _find_operation(prov, "generate")
    return _Call(provider=canon or given or "spritewarden", operation=name or "generate", tool="generate",
                 via="warden", args=args, prov=prov, entry=entry)


def _classify_mcp(tool_name: str, tool_input: dict[str, Any], reg: dict[str, Any]) -> _Call | None:
    for pname, prov in (reg.get("providers") or {}).items():
        enf = _enforce(prov)
        for prefix in enf.get("mcp_prefixes", []):
            if not tool_name.startswith(prefix):
                continue
            short = tool_name[len(prefix):]
            if _READ_ONLY.search(short):
                return None
            spends = enf.get("spends")
            if spends is not None and not any(re.search(p, short) for p in spends):
                return None
            if any(re.search(p, short) for p in enf.get("read_only", [])):
                return None
            op = short
            for ov in enf.get("operation_overrides", []):
                if ov.get("tool") == short and tool_input.get(ov.get("arg_present", "")):
                    op = ov.get("operation", op)
            name, entry = _find_operation(prov, op)
            return _Call(provider=pname, operation=name or op, tool=short, via="mcp", args=dict(tool_input),
                         prov=prov, entry=entry)
    return None


# ----------------------------------------------------------------------------- manifest + sources

class _Manifest:
    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        self._data: Any = None
        self._loaded = False

    @property
    def data(self) -> dict[str, Any]:
        if not self._loaded:
            self._loaded = True
            try:
                raw = json.loads(self.cfg.manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
            self._data = raw if isinstance(raw, dict) else {}
        return self._data

    def _entries(self):
        for value in self.data.values():
            if isinstance(value, list):
                candidates = value
            elif isinstance(value, dict):
                candidates = [value, *[v for v in value.values() if isinstance(v, dict)]]
            else:
                continue
            for entry in candidates:
                if isinstance(entry, dict):
                    yield entry

    def asset_file(self, asset_id: str) -> Path | None:
        """The local file a provider asset id was downloaded to."""
        if not asset_id:
            return None
        for entry in self._entries():
            if entry.get("asset_id") == asset_id and entry.get("file"):
                return self.cfg.resolve(str(entry["file"]))
        return None

    def url_file(self, url: str) -> Path | None:
        """A file registered for this exact URL, or for the SpriteCook asset id inside it."""
        for entry in self._entries():
            urls = [entry.get("url"), *(entry.get("urls") or [])] if isinstance(entry.get("urls"), list) else [entry.get("url")]
            if url in urls and entry.get("file"):
                return self.cfg.resolve(str(entry["file"]))
        m = _ASSET_URL.search(url)
        if not m:
            return None
        return self.asset_file(m.group(1)) or self.asset_file(m.group(1).lower())

    def character_dir(self, character_id: str) -> Path | None:
        chars = self.data.get("pixellab_characters")
        if isinstance(chars, dict):
            chars = [{"character_id": k, **v} if isinstance(v, dict) else {"character_id": k, "dir": v} for k, v in chars.items()]
        for entry in chars or []:
            if isinstance(entry, dict) and entry.get("character_id") == character_id and entry.get("dir"):
                return self.cfg.resolve(str(entry["dir"]))
        return None


def _values(value: Any) -> list[str]:
    if value in (None, "", True, False):
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v not in (None, "", True, False)]
    return [str(value)]


def _resolve_sources(call: _Call, cfg: ProjectConfig) -> tuple[list[_Source], list[str]]:
    """The call's source images as local files, plus deny reasons for sources that cannot be resolved."""
    specs = list(_enforce(call.prov).get("source", [])) + list(_enforce(call.entry).get("source", []))
    sources: list[_Source] = []
    reasons: list[str] = []
    manifest = _Manifest(cfg)
    where = cfg.relative(cfg.manifest_path)
    for spec in specs:
        kind = spec.get("kind")
        found_any = False
        for arg in spec.get("args", []):
            for value in _values(call.args.get(arg)):
                found_any = True
                if kind == "path":
                    sources.append(_Source(cfg.resolve(value), "file", value))
                elif kind == "manifest_asset":
                    path = manifest.asset_file(value)
                    if path is None:
                        reasons.append(
                            f"the source asset id ({arg}={value}) is not in {where} with a local file: register it "
                            f"(download + manifest entry {{\"asset_id\", \"file\"}}) and gate it before animating.")
                    else:
                        sources.append(_Source(path, "file", f"{arg}={value}"))
                elif kind == "url":
                    path = manifest.url_file(value)
                    if path is None:
                        reasons.append(
                            f"{arg} ({value[:120]}) does not resolve to a file in {where}: register the gated file "
                            f"and its URL in the manifest (an entry {{\"url\" or \"asset_id\", \"file\"}}) so the "
                            f"reference can be checked against its gate record.")
                    else:
                        sources.append(_Source(path, "file", arg))
                elif kind == "pixellab_character":
                    path = manifest.character_dir(value)
                    if path is None:
                        reasons.append(
                            f"PixelLab character {value} is not in {where} under \"pixellab_characters\" "
                            f"({{\"character_id\", \"dir\"}}): download its views, gate them with `warden judge-views` "
                            f"and register the folder before animating it.")
                    else:
                        sources.append(_Source(path, "views_dir", f"{arg}={value}"))
                else:
                    raise ValueError(f"unknown source kind {kind!r} in the registry")
            if found_any and kind == "manifest_asset":
                break  # the first id argument given names the source
        if not found_any and kind == "manifest_asset":
            reasons.append(
                f"no source asset id given ({' / '.join(spec.get('args', []))}): the call must name an asset "
                f"registered in {where} so its gate record can be checked.")
    return sources, reasons


# ----------------------------------------------------------------------------- gates + pipeline

def accepted_gates(required: list[str] | tuple[str, ...] | None) -> list[str]:
    """The recipe's listed gates (with their aliases) plus the umbrella names, in a stable order."""
    out: list[str] = []
    for g in list(required or []) + list(UMBRELLA_GATES):
        for name in (g, GATE_ALIASES.get(g)):
            if name and name not in out:
                out.append(name)
    return out


def gate_reasons(path: str | Path, needed: list[str] | tuple[str, ...], cfg: ProjectConfig) -> list[str]:
    """Deny reasons when ``path`` has no current passed record for any of ``needed`` (empty list = gated)."""
    from ..gates.records import RECORD_SUFFIX, load_record, passed_gates, sha256_of

    path = Path(path)
    rel = cfg.relative(path)
    if not path.is_file():
        return [f"source image not found locally: {rel}"]
    record = load_record(path)
    how = (f"Run `warden judge-still --start-image` (or judge-views / judge-anim) on it first: nothing derives "
           f"from an un-gated anchor.")
    if record is None:
        return [f"{rel} has no gate record (<image>{RECORD_SUFFIX}). {how}"]
    try:
        current = record.get("sha256") == sha256_of(path)
    except OSError:
        current = False
    if not current:
        return [f"{rel} changed since it was gated (sha256 mismatch with its {RECORD_SUFFIX}). {how}"]
    passed = passed_gates(path)
    if not any(g in passed for g in needed):
        have = f" (passed: {', '.join(sorted(passed))})" if passed else " (nothing passed)"
        return [f"{rel} has no current passed gate among {' / '.join(needed)}{have}. {how}"]
    return []


def _facing_files(folder: Path) -> list[Path]:
    out = []
    try:
        files = sorted(folder.iterdir())
    except OSError:
        return out
    for f in files:
        if f.suffix.lower() != ".png":
            continue
        tokens = f.stem.lower().replace("-", "_").replace(" ", "_").split("_")
        if any("_".join(tokens[-n:]) in FACING_STEMS for n in (3, 2, 1) if len(tokens) >= n):
            out.append(f)
    return out


def _views_dir_reasons(folder: Path, cfg: ProjectConfig) -> list[str]:
    from ..gates.records import passed_gates

    rel = cfg.relative(folder)
    files = _facing_files(folder)
    if not files:
        return [f"{rel} holds no <facing>.png views: download the character's directions there and gate them "
                f"with `warden judge-views {rel}`."]
    missing = [f.name for f in files if not any(g in passed_gates(f) for g in VIEW_GATES)]
    if missing:
        more = f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""
        return [f"{len(missing)} of {len(files)} view(s) in {rel} lack a current passed `views` (direction_set) "
                f"gate: {', '.join(missing[:8])}{more}. Run `warden judge-views {rel}` first: no motion derives "
                f"from an un-gated direction set."]
    return []


def find_pipeline(path: Path, max_levels: int = 8) -> tuple[dict[str, Any] | None, Path | None]:
    """The nearest ``pipeline.json`` walking up from ``path`` (a file or a folder)."""
    d = path if path.is_dir() else path.parent
    for _ in range(max_levels):
        candidate = d / "pipeline.json"
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None, candidate
            return (data if isinstance(data, dict) else None), candidate
        if d.parent == d:
            break
        d = d.parent
    return None, None


def pipeline_reasons(path: Path, provider: str, stage: str | None, cfg: ProjectConfig, reg: dict[str, Any]) -> list[str]:
    if not stage:
        return []
    pipeline, where = find_pipeline(path)
    if not pipeline or where is None:
        return []
    chosen = pipeline.get(f"{stage}_provider")
    if not chosen:
        return []
    mine = _canonical_provider(reg, provider) or _norm(provider)
    theirs = _canonical_provider(reg, chosen) or _norm(chosen)
    if mine == theirs:
        return []
    return [f"{cfg.relative(where)} says {stage} is done by {chosen}, not {provider}: never mix providers inside "
            f"one set. Change pipeline.json deliberately if the route changed."]


# ----------------------------------------------------------------------------- rules

class _Fields(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _message(rule: dict[str, Any], default: str, **fields: Any) -> str:
    return str(rule.get("message") or default).format_map(_Fields(fields))


def _present(value: Any) -> bool:
    return value is not None and value != "" and value is not False


def _rule_max_items(rule, call, sources, cfg, reg) -> list[str]:
    value = call.args.get(rule["arg"])
    if isinstance(value, (list, tuple)) and len(value) > int(rule["max"]):
        return [_message(rule, "`{arg}` has {count} entries; the limit is {max}.",
                         arg=rule["arg"], count=len(value), max=rule["max"])]
    return []


def _rule_forbid_args(rule, call, sources, cfg, reg) -> list[str]:
    out = []
    for arg in rule.get("args", []):
        if _present(call.args.get(arg)):
            out.append(_message(rule, "`{arg}` is not accepted by {provider} `{operation}`.",
                                arg=arg, provider=call.provider, operation=call.operation))
    return out


def _rule_enum(rule, call, sources, cfg, reg) -> list[str]:
    value = call.args.get(rule["arg"])
    if value is None or value is True or value == "":
        return []
    allowed = {str(v).lower() for v in rule.get("values", [])}
    if str(value).strip().lower() not in allowed:
        return [_message(rule, "`{arg}` must be one of {values}; got {value}.",
                         arg=rule["arg"], values=", ".join(str(v) for v in rule.get("values", [])), value=value)]
    return []


def _rule_multiple_of(rule, call, sources, cfg, reg) -> list[str]:
    value = call.args.get(rule["arg"])
    if value is None or value is True:
        return []
    of = int(rule["of"])
    dims = [int(d) for d in re.findall(r"\d+", " ".join(_values(value)) if isinstance(value, (list, tuple)) else str(value))]
    if dims and any(d % of for d in dims):
        return [_message(rule, "`{arg}` must be a multiple of {of}; got {value}.", arg=rule["arg"], of=of, value=value)]
    return []


def _rule_forbid_inline_image_args(rule, call, sources, cfg, reg) -> list[str]:
    used = [a for a in rule.get("args", []) if call.args.get(a)]
    if not used:
        return []
    text = _message(rule, "inline base64 images are not accepted: pass the URL field instead.", args=", ".join(used))
    return [f"{', '.join(used)}: {text}"]


def _prompt_texts(rule: dict[str, Any], call: _Call) -> list[tuple[str, str]]:
    return [(a, str(call.args[a])) for a in rule.get("args", PROMPT_ARGS) if isinstance(call.args.get(a), str)]


def _rule_forbidden_words(rule, call, sources, cfg, reg) -> list[str]:
    patterns = list((call.prov.get("defaults") or {}).get("forbidden_words", []))
    patterns += [p for p in (call.entry or {}).get("forbidden_words", []) if p not in patterns]
    patterns += [p for p in rule.get("patterns", []) if p not in patterns]
    label = PROVIDER_LABELS.get(call.provider, call.provider)
    seen: set[str] = set()
    out = []
    for arg, text in _prompt_texts(rule, call):
        for pattern in patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if not m:
                continue
            word = m.group(0).strip().lower()
            if word in seen:
                continue
            seen.add(word)
            out.append(_message(rule, "{label}'s documentation forbids \"{word}\" in a prompt (`{arg}`): remove it.",
                                label=label, word=word, arg=arg))
    return out


def _rule_no_dimensions_in_prompt(rule, call, sources, cfg, reg) -> list[str]:
    for arg, text in _prompt_texts(rule, call):
        if any(p.search(text) for p in _DIMENSIONS):
            return [_message(rule, "no pixel dimensions in the prompt (`{arg}`); size goes in the size field.", arg=arg)]
    return []


def _rule_loop_engine(rule, call, sources, cfg, reg) -> list[str]:
    if call.args.get(rule.get("model_arg", "model")) != rule.get("model"):
        return []
    words = "|".join(re.escape(w) for w in rule.get("words", []))
    if not words:
        return []
    pattern = re.compile(r"\b(" + words + r")", re.IGNORECASE)
    for arg, text in _prompt_texts(rule, call):
        m = pattern.search(text)
        if m:
            return [_message(rule, "{model} must not be used for a looping motion (\"{word}\").",
                             model=rule.get("model"), word=m.group(1).lower(), arg=arg)]
    return []


def _loose(value: Any) -> Any:
    if isinstance(value, str) and value.strip().lower() in ("false", "0", "no", "off"):
        return False
    if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes", "on"):
        return True
    return value


def _rule_require_arg_value(rule, call, sources, cfg, reg) -> list[str]:
    arg = rule["arg"]
    if arg not in call.args or call.args[arg] is None:
        return []
    value = _loose(call.args[arg])
    bad = any(value is b or (not isinstance(b, bool) and value == b) for b in rule.get("not", []))
    allowed = rule.get("equals")
    if allowed is not None and not any(value is a or (not isinstance(a, bool) and value == a) for a in allowed):
        bad = True
    if bad:
        return [_message(rule, "`{arg}` must not be {value}.", arg=arg, value=call.args[arg])]
    return []


def _rule_matte_collision(rule, call, sources, cfg, reg) -> list[str]:
    removebg_arg = rule.get("removebg_arg")
    if removebg_arg and str(call.args.get(removebg_arg, "")).strip().lower() == "none":
        return []  # nothing is keyed out
    source = next((s for s in sources if s.kind == "file" and s.path.is_file()), None)
    if source is None:
        return []  # an unresolved source is already a deny
    matte_arg = rule.get("matte_arg")
    matte = (call.args.get(matte_arg) if matte_arg else None) or rule.get("default", "#808080")
    distance = float(rule.get("distance", 24))
    limit = float(rule.get("max_fraction", 0.02))
    from .images import matte_fraction  # PIL is imported only here

    fraction, opaque = matte_fraction(source.path, str(matte), distance)
    if opaque and fraction > limit:
        advice = rule.get("advice") or "pass a matte colour absent from the sprite (e.g. \"#FF00FF\")."
        return [
            f"{fraction * 100:.1f}% of the opaque pixels of {cfg.relative(source.path)} are within RGB distance "
            f"{distance:g} of the matte colour {matte} (limit {limit * 100:g}%): background removal would key those "
            f"pixels out of every animation frame (the default grey matte removed the grey fill of a grey robot's "
            f"head). {advice}"
        ]
    return []


_RULES: dict[str, Callable[..., list[str]]] = {
    "max_items": _rule_max_items,
    "forbid_args": _rule_forbid_args,
    "enum": _rule_enum,
    "multiple_of": _rule_multiple_of,
    "forbid_inline_image_args": _rule_forbid_inline_image_args,
    "forbidden_words": _rule_forbidden_words,
    "no_dimensions_in_prompt": _rule_no_dimensions_in_prompt,
    "loop_engine": _rule_loop_engine,
    "require_arg_value": _rule_require_arg_value,
    "matte_collision": _rule_matte_collision,
}


def _evaluate(call: _Call, cfg: ProjectConfig, reg: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    sources, source_reasons = _resolve_sources(call, cfg)
    rules = list(_enforce(call.prov).get("rules", [])) + list(_enforce(call.entry).get("rules", []))
    for rule in rules:
        fn = _RULES.get(rule.get("kind"))
        if fn is None:
            raise ValueError(f"unknown enforce rule kind {rule.get('kind')!r} in the registry")
        reasons += fn(rule, call, sources, cfg, reg)
    reasons += source_reasons
    needed = accepted_gates((call.entry or {}).get("required_gates_before"))
    stage = _enforce(call.entry).get("stage")
    for source in sources:
        if source.kind == "views_dir":
            reasons += _views_dir_reasons(source.path, cfg)
        else:
            reasons += gate_reasons(source.path, needed, cfg)
        if source.path.exists():
            reasons += pipeline_reasons(source.path, call.provider, stage, cfg, reg)
    return list(dict.fromkeys(reasons))


RECIPE_INPUT_KEYS = ("input", "inputs", "input_image", "reference_image", "start_image", "source_image", "image")


def recipe_gate_reasons(
    provider: str,
    operation: str,
    params: dict[str, Any] | None,
    project_root: str | Path,
    *,
    registry: dict[str, Any] | None = None,
) -> list[str]:
    """Why a recipe must not run yet: its input image is missing, un-gated or belongs to another provider's set.

    Used by ``generator.registry.dispatch``. Only operations that derive from an image (a
    registry entry with ``enforce.stage`` and ``required_gates_before``) are gated; stills and
    edits create anchors and are never refused here. The input is resolved like the hook does
    (asset ids / URLs / character ids through the manifest) plus the recipe's local path
    params (``RECIPE_INPUT_KEYS``). Raises nothing for unknown recipes: they have no gates.
    """
    reg = registry if registry is not None else load_raw_registry()
    canon = _canonical_provider(reg, provider)
    prov = (reg.get("providers") or {}).get(canon, {}) if canon else {}
    name, entry = _find_operation(prov, operation)
    if entry is None or not entry.get("required_gates_before") or not _enforce(entry).get("stage"):
        return []
    cfg = load_config(project_root)
    args = dict(params or {})
    call = _Call(provider=canon or provider, operation=name or operation, tool=operation, via="recipe",
                 args=args, prov=prov, entry=entry)
    sources, reasons = _resolve_sources(call, cfg)
    known = {s.path for s in sources}
    for key in RECIPE_INPUT_KEYS:
        for value in _values(args.get(key)):
            if value.startswith(("http://", "https://", "data:")):
                continue
            path = cfg.resolve(value)
            if path not in known:
                known.add(path)
                sources.append(_Source(path, "file", f"{key}={value}"))
    needed = accepted_gates(entry.get("required_gates_before"))
    if not sources and not reasons:
        return [
            f"{call.provider}/{call.operation} requires a gated input image ({' / '.join(entry['required_gates_before'])}) "
            f"but the recipe names none: pass params.input_image (a local file with a passed gate record) or a "
            f"registered asset / character id."
        ]
    stage = _enforce(entry).get("stage")
    for source in sources:
        if source.kind == "views_dir":
            reasons += _views_dir_reasons(source.path, cfg)
        else:
            reasons += gate_reasons(source.path, needed, cfg)
        if source.path.exists():
            reasons += pipeline_reasons(source.path, call.provider, stage, cfg, reg)
    return list(dict.fromkeys(reasons))


# ----------------------------------------------------------------------------- override + context

def _take_override(cfg: ProjectConfig) -> str | None:
    """Spend one override call; the reason text, or None when there is no override left."""
    path = cfg.override_path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        calls = int(data.get("calls", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if calls <= 0:
        return None
    data["calls"] = calls - 1
    try:
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass
    return str(data.get("reason") or "(no reason given)")


def _recipe_context(call: _Call) -> str:
    """The recipe's prompt rules and start-image notes for this call, compact."""
    entry = call.entry or {}
    defaults = call.prov.get("defaults") or {}
    rules = list(entry.get("prompt_rules", []))
    rules += [r for r in defaults.get("prompt_rules", []) if r not in rules]
    lines = [f"GENERATOR CALL ({call.provider} / {call.operation}): check it against the recipe before it spends."]
    if entry.get("description"):
        lines.append(str(entry["description"]))
    si = entry.get("start_image") or {}
    if si.get("required"):
        bits = []
        if si.get("facing") and si["facing"] != "any":
            bits.append(f"faces {si['facing']}")
        if si.get("canvas") and si["canvas"] != "any":
            bits.append(f"{si['canvas']} canvas")
        if si.get("margin_px"):
            bits.append(f">={si['margin_px']}px margin")
        if si.get("max_px"):
            bits.append(f"<={si['max_px']}px")
        if si.get("multiple_of"):
            bits.append(f"side a multiple of {si['multiple_of']}")
        if si.get("native_pixels", True):
            bits.append("native 1:1 pixels")
        if si.get("pose"):
            bits.append(f"pose: {si['pose']}")
        lines.append("Start image: " + "; ".join(bits) + ".")
        lines += [f"- {n}" for n in si.get("notes", [])]
    if rules:
        lines.append("Prompt rules:")
        lines += [f"- {r}" for r in rules]
    before = entry.get("required_gates_before") or []
    after = entry.get("required_gates_after") or []
    if before or after:
        lines.append(f"Gates before: {', '.join(before) or '-'}; gates after: {', '.join(after) or '-'}.")
    if not call.entry:
        lines.append(f"(no recipe entry for {call.provider}/{call.operation}: `warden recipes {call.provider}` lists the known operations.)")
    text = "\n".join(lines)
    if len(text) > CONTEXT_LIMIT:
        text = text[:CONTEXT_LIMIT].rstrip() + f"\n...(truncated; `warden recipes {call.provider} {call.operation}`)"
    return text


def _finish(provider: str, operation: str, reasons: list[str], cfg: ProjectConfig, context: str) -> Verdict:
    if not reasons:
        return Verdict("allow", provider, operation, [], context)
    why = _take_override(cfg)
    if why is None:
        return Verdict("deny", provider, operation, reasons, "")
    return Verdict(
        "allow", provider, operation, reasons,
        f"sprite-warden: {len(reasons)} rule(s) overridden for this call (reason: {why}): " + " | ".join(reasons),
    )


# ----------------------------------------------------------------------------- review pages

def _review(tool_input: dict[str, Any], cfg: ProjectConfig, state: dict[str, Any]) -> Verdict:
    from ..gates.records import passed_gates

    file_path = str(tool_input.get("file_path") or "")
    if not cfg.review_specs or not file_path.lower().endswith(".html"):
        return Verdict("ignore")
    page = cfg.resolve(file_path)
    spec_path = page.with_suffix(".json")
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except OSError:
        return Verdict("ignore")
    except ValueError:
        return Verdict("ignore")
    if not isinstance(spec, dict) or not isinstance(spec.get("groups"), list):
        return Verdict("ignore")
    state["spending"] = True
    state["provider"], state["operation"] = "review", "publish"
    if cfg.error:
        raise ValueError(cfg.error)
    missing: list[str] = []
    for group in spec["groups"]:
        for item in (group or {}).get("items", []) or []:
            for p in (item or {}).get("paths", []) or []:
                candidates = [cfg.resolve(str(p)), spec_path.parent / str(p)]
                full = next((c for c in candidates if c.is_file()), candidates[0])
                if not any(g in passed_gates(full) for g in REVIEW_GATES) and str(p) not in missing:
                    missing.append(str(p))
    reasons = []
    if missing:
        shown = ", ".join(missing[:20]) + (f" (+{len(missing) - 20} more)" if len(missing) > 20 else "")
        reasons.append(
            f"{len(missing)} review page candidate(s) without a current passed gate record "
            f"({' / '.join(REVIEW_GATES)}): {shown}. Gate them (warden judge-still / judge-views / judge-anim) "
            f"before anyone sees them.")
    return _finish("review", "publish", reasons, cfg, "")


# ----------------------------------------------------------------------------- entry point

def check_call(
    tool_name: str,
    tool_input: dict[str, Any] | None,
    project_root: str | Path,
    *,
    registry: dict[str, Any] | None = None,
) -> Verdict:
    """Decide one tool call. Never raises: an internal error on a spending call is an ``allow`` whose
    context starts with ``gatekeeper error:`` (fail open, loudly)."""
    state: dict[str, Any] = {"spending": False, "provider": None, "operation": None}
    try:
        name = str(tool_name or "")
        args = tool_input if isinstance(tool_input, dict) else {}
        if name not in ("Bash", "PowerShell", "Artifact") and not name.startswith("mcp__"):
            return Verdict("ignore")
        cfg = load_config(project_root)
        if name == "Artifact":
            return _review(args, cfg, state)

        def get_reg() -> dict[str, Any]:
            return registry if registry is not None else load_raw_registry()

        if name in ("Bash", "PowerShell"):
            call = _classify_command(str(args.get("command") or ""), cfg, get_reg)
        else:
            call = _classify_mcp(name, args, get_reg())
        if call is None:
            return Verdict("ignore")
        state.update(spending=True, provider=call.provider, operation=call.operation)
        if cfg.error:
            raise ValueError(cfg.error)
        reasons = _evaluate(call, cfg, get_reg())
        return _finish(call.provider, call.operation, reasons, cfg, _recipe_context(call))
    except Exception as exc:  # noqa: BLE001 - the gatekeeper must never break the caller
        if not state["spending"]:
            return Verdict("ignore")
        return Verdict("allow", state["provider"], state["operation"], [],
                       f"gatekeeper error: {type(exc).__name__}: {exc}")

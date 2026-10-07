"""Enforcement: deny generator calls that break a provider's recipe or skip a gate.

``check_call(tool_name, tool_input, project_root)`` is the single entry point,
used by ``warden hook --claude`` (a Claude Code PreToolUse hook), ``warden
check-call`` and ``generator.registry.dispatch``. The rules live in the recipe
registry (``data/recipes.json``: the ``enforce`` blocks), so the registry is the
one enforced source.

This package imports nothing heavy at module level (no numpy / PIL / httpx /
pydantic / mcp): a call that is not a spending generator call must be answered
in a few milliseconds.
"""

from .config import ProjectConfig, find_project_root, load_config
from .core import RULE_KINDS, UMBRELLA_GATES, Verdict, check_call, gate_reasons, recipe_gate_reasons

__all__ = [
    "recipe_gate_reasons",
    "ProjectConfig",
    "RULE_KINDS",
    "UMBRELLA_GATES",
    "Verdict",
    "check_call",
    "find_project_root",
    "gate_reasons",
    "load_config",
]

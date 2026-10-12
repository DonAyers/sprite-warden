"""Console entry point (``warden`` / ``spritewarden`` / ``sprite-warden``).

Deliberately tiny: the enforcement commands (``hook``, ``check-call``) run on every
tool call of an agent session, so they are dispatched before the full CLI and its
imports (rich, PIL, httpx, pydantic) load. Everything else lives in ``cli_full``.
"""

from __future__ import annotations

import sys

FAST_COMMANDS = ("hook", "check-call", "rules", "pipeline")


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "workflow":
        from .workflow_cli import main as workflow_main

        sys.exit(workflow_main(args[1:]))
    if args and args[0] in FAST_COMMANDS:
        from .enforce.cli import main as enforce_main

        sys.exit(enforce_main(args))
    from .cli_full import main as full_main

    full_main(args)


def __getattr__(name: str):
    """Keep ``spritewarden.cli.<anything>`` working for callers of the old single-module CLI."""
    if name.startswith("__"):
        # the import system probes __path__ etc.; answering must not load the full CLI
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import cli_full

    try:
        return getattr(cli_full, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


if __name__ == "__main__":
    main()

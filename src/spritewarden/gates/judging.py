"""Shared, import-light operation correlation for CLI and MCP judgments."""
from functools import wraps
from inspect import signature
from pathlib import Path


def judgment_operation(fn):
    sig = signature(fn, eval_str=True)

    @wraps(fn)
    def wrapped(*args, **kwargs):
        from ..events import operation
        from ..enforce.config import find_project_root
        bound = sig.bind(*args, **kwargs).arguments
        params = vars(bound['args']) if 'args' in bound else dict(bound)
        source = next((params[k] for k in ('image', 'file_path', 'sheet_or_dir_path', 'path', 'source_path', 'source')
                       if params.get(k)), None)
        path = Path(source).resolve() if source else Path.cwd()
        root = params.get('project') or find_project_root(path if path.is_dir() else path.parent)
        with operation(fn.__name__, project_root=root, payload={'params': params}):
            return fn(*args, **kwargs)

    # MCP introspects the concrete resolved signature, not this module's globals.
    wrapped.__signature__ = sig
    wrapped.__annotations__ = {k: v.annotation for k, v in sig.parameters.items()}
    wrapped.__annotations__['return'] = sig.return_annotation
    return wrapped

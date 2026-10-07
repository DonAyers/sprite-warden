"""Gate records: sidecar proof that an image passed the judges."""

from .records import (
    RECORD_SUFFIX,
    gates_from_scorecard,
    is_gated,
    load_record,
    passed_gates,
    record_path,
    require_passed,
    review_problem,
    sha256_of,
    write_record,
)

__all__ = [
    "review_problem",
    "RECORD_SUFFIX",
    "gates_from_scorecard",
    "is_gated",
    "load_record",
    "passed_gates",
    "record_path",
    "require_passed",
    "sha256_of",
    "write_record",
]

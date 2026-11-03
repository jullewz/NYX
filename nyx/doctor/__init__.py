"""nyx doctor — finds problems in Python and NYX code and fixes what it safely can."""

from .core import Issue  # noqa: F401
from .engine import detect_language, diagnose  # noqa: F401

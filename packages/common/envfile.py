"""Minimal .env loader. Fills in environment variables that are not already set; never logs values."""
import os
from pathlib import Path
from typing import List, Optional, Union

DEFAULT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def load_env_defaults(path: Optional[Union[str, Path]] = None) -> List[str]:
    """Set variables from a KEY=value file without overriding the real environment.

    Returns the names that were set, so callers can report what changed without ever printing a value.
    """
    env_file = Path(path) if path else DEFAULT_ENV_FILE
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    applied: List[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name and name not in os.environ:
            os.environ[name] = value
            applied.append(name)
    return applied

"""Fleet machine agent (supervisor). Standard library only; shipped as a tarball to machines."""

from __future__ import annotations

import os


def get_version() -> str:
    """Return the agent version from fleetagent/VERSION, or "dev" when the file is absent."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
    except OSError:
        return "dev"
    return value or "dev"


__version__ = get_version()

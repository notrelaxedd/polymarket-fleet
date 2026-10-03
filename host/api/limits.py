"""Size limits on worker-supplied JSON payloads (checkpoints, results)."""
from __future__ import annotations

import json
from typing import Any

MAX_PAYLOAD_BYTES = 64 * 1024
MAX_JOB_ENTRIES = 64


def small_payload(value: Any, what: str = "payload") -> Any:
    """Pydantic validator body: refuse a JSON value above MAX_PAYLOAD_BYTES when encoded."""
    if value is not None and len(json.dumps(value)) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"{what} larger than {MAX_PAYLOAD_BYTES} bytes")
    return value

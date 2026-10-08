"""The /stocks dashboard page and its forms (docs/ALPACA.md "Step 9", contract
tools/workflows/step9-contract.txt)."""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["stocks-dashboard"])

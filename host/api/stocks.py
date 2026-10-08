"""Stock routes (docs/ALPACA.md "Step 9", contract tools/workflows/step9-contract.txt):
worker routes under /api/v1 (bars, trade state, order requests, release) and owner JSON
routes under /api/stocks."""
from __future__ import annotations

from fastapi import APIRouter

worker_router = APIRouter(prefix="/api/v1", tags=["stocks-worker"])
owner_router = APIRouter(prefix="/api/stocks", tags=["stocks-owner"])

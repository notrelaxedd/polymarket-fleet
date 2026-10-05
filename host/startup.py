"""Work the host does once at startup, after the migrations and before serving.

recompute_statuses re-judges every lineage against the gates in force. A migration
can make a gate stricter (0006 made the backtest gate judge the held-out validation
era, which no step 5 lineage has); without this a paper_ok or live_eligible lineage
would keep its status until its next settlement or a thresholds save, and a live
assignment could keep trading a lineage the gates no longer pass. It is the same
recompute a thresholds save runs (backtest gate on every lineage, paper gate on every
lineage with a paper record), so a demoted live_eligible lineage has its live
assignments halted and their orders cancelled. Running it on every start is safe:
it only moves statuses to what the gates say.
"""
from __future__ import annotations

import logging
from typing import Any

import psycopg

from host import db
from host.eligibility import recompute_all, recompute_paper

log = logging.getLogger("host.startup")
ACTOR = "startup"


def recompute_statuses(conn: psycopg.Connection) -> dict[str, Any]:
    """Recompute every lineage's status; {"lineages", "paper", "changed"}."""
    before = {r["lineage_id"]: r["status"] for r in conn.execute("SELECT lineage_id, status FROM models WHERE id = lineage_id")}
    lineages = recompute_all(conn)
    paper = conn.execute("SELECT DISTINCT lineage_id FROM model_scores WHERE mode = 'paper'").fetchall()
    for row in paper:
        recompute_paper(conn, row["lineage_id"], ACTOR)
    after = {r["lineage_id"]: r["status"] for r in conn.execute("SELECT lineage_id, status FROM models WHERE id = lineage_id")}
    changed = sorted(str(lid) for lid, status in after.items() if before.get(lid) != status)
    return {"lineages": lineages, "paper": len(paper), "changed": changed}


def run(database_url: str) -> dict[str, Any]:
    """recompute_statuses in its own transaction (host.main calls this after migrate)."""
    with db.connect(database_url) as conn:
        out = recompute_statuses(conn)
    log.info("startup eligibility recompute: %d lineages, %d with paper records, changed %s",
             out["lineages"], out["paper"], out["changed"] or "none")
    return out

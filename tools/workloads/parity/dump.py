"""Normalized rows of one parity run, and the A versus B diff.

Normalization (every rule is about identity or wall time, never about trading values):
- uuids (assignments, models, lineages, markets, jobs, bankrolls, orders) become stable
  labels: A1.. by game_id, M1.. by creation (root then trained child), K:<market_ref>,
  B:<assignment>, J:train:<n> / J:trade:<assignment>, O1.. by (assignment, creation),
  F1.. by (order, id). Worker ids become W1. Labels also replace ids inside text and
  JSON (rationale, details, exchange fill ids, ledger ref ids).
- Every timestamp and date column is dropped, and so are ISO times inside JSON values.
- Snapshot ids (bigserial, so their number depends on how many snapshots the exchange
  took before the worker looked) become the snapshot's content: market label, bid, ask
  and a hash of both depth lists. With the frozen sim clock every snapshot of a market has
  the same content, so this keeps "which book was used" and drops "which poll".
- Bigserial row ids are dropped; client_request_id (a hash over uuids and the snapshot
  id) becomes crid:<order label>, also inside the paper exchange order id paper:<crid>.
- Row order is canonical: per assignment or bankroll (or order), then by id. Rows of
  different assignments interleave by timing, so they are never compared by global id.
- A fill gets `after_order_snapshot`: its snapshot is newer than its order's (the paper
  rule), the one fact about snapshot numbers that does not depend on timing.
"""
from __future__ import annotations

import datetime as dt
import decimal
import difflib
import hashlib
import json
import re
import uuid
from typing import Any

from host import db

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:?\d{2}|Z)?$")
PAPER_FILL_RE = re.compile(r"^paper:(?P<order>[^:]+):(?P<snap>\d+):(?P<level>b?\d+)$")
TABLES = ("orders", "order_events", "fills", "ledger", "bets", "model_scores", "bankrolls", "models", "jobs")
DROP = {
    "orders": {"id"},
    "order_events": {"id"},
    "fills": {"id"},
    "ledger": {"id"},
    "bets": {"id"},
    "jobs": {"id", "lease_token", "lease_worker_id", "checkpoint", "progress", "expiries", "lease_expires_at"},
    "models": {"id"},
    "bankrolls": {"id"},
}


class Labels:
    """uuid / worker id / snapshot id / fill id -> stable label."""

    def __init__(self) -> None:
        self.ids: dict[str, str] = {}
        self.snapshots: dict[int, str] = {}
        self.fills: dict[str, str] = {}
        self.order_snapshot: dict[str, int | None] = {}

    def add(self, raw: Any, label: str) -> None:
        if raw is not None:
            self.ids.setdefault(str(raw), label)

    def text(self, value: str) -> str:
        if value in self.ids:
            return self.ids[value]
        if value.startswith("paper:") and value[6:] in self.ids:  # a paper exchange order id
            return "paper:" + self.ids[value[6:]]
        match = PAPER_FILL_RE.match(value)
        if match:
            snap = self.snapshots.get(int(match["snap"]), "S?")
            return f"paper:{self.text(match['order'])}:{snap}:{match['level']}"
        if ISO_RE.match(value):
            return "<time>"
        return UUID_RE.sub(lambda m: self.ids.get(m.group(0), "<unknown-uuid>"), value)

    def value(self, value: Any) -> Any:
        if isinstance(value, (dt.datetime, dt.date)):
            return "<time>"
        if isinstance(value, uuid.UUID):
            return self.ids.get(str(value), "<unknown-uuid>")
        if isinstance(value, decimal.Decimal):
            return str(value.normalize()) if value == value.to_integral() else str(value)
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(str(k)): self.value(v) for k, v in sorted(value.items())}
        if isinstance(value, (list, tuple)):
            return [self.value(v) for v in value]
        if isinstance(value, float):
            return repr(value)
        return value


def _rows(conn: Any, sql: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql).fetchall()]


def build_labels(conn: Any) -> Labels:
    lab = Labels()
    for i, r in enumerate(_rows(conn, "SELECT id FROM workers ORDER BY registered_at, id"), 1):
        lab.add(r["id"], f"W{i}")
    models = _rows(conn, "SELECT id, lineage_id FROM models ORDER BY created_at, parent_model_id NULLS FIRST, id")
    for i, r in enumerate(models, 1):
        lab.add(r["id"], f"M{i}")
    for r in _rows(conn, "SELECT id, market_ref FROM markets ORDER BY game_id, side, market_ref"):
        lab.add(r["id"], f"K:{r['market_ref']}")
    assignments = _rows(conn, "SELECT a.id, a.job_id, b.id AS bankroll_id FROM assignments a "
                              "LEFT JOIN bankrolls b ON b.assignment_id = a.id ORDER BY a.game_id, a.created_at, a.id")
    for i, r in enumerate(assignments, 1):
        lab.add(r["id"], f"A{i}")
        lab.add(r["bankroll_id"], f"B:A{i}")
        lab.add(r["job_id"], f"J:trade:A{i}")
    for i, r in enumerate(_rows(conn, "SELECT id, kind FROM jobs WHERE kind <> 'trade' ORDER BY created_at, id"), 1):
        lab.add(r["id"], f"J:{r['kind']}:{i}")
    for r in _rows(conn, "SELECT s.id, s.bid, s.ask, s.bid_depth, s.ask_depth, s.market_id FROM price_snapshots s"):
        depth = json.dumps([r["bid_depth"], r["ask_depth"]], sort_keys=True, default=str)
        digest = hashlib.sha256(depth.encode()).hexdigest()[:8]
        lab.snapshots[int(r["id"])] = f"S[{lab.ids.get(str(r['market_id']), '?')}|bid={r['bid']}|ask={r['ask']}|{digest}]"
    orders = _rows(conn, "SELECT id, assignment_id, client_request_id, snapshot_id FROM orders ORDER BY created_at, id")
    lab.order_snapshot = {str(r["id"]): r["snapshot_id"] for r in orders}
    order_key = sorted(range(len(orders)), key=lambda k: (lab.ids.get(str(orders[k]["assignment_id"]), "~"), k))
    for i, k in enumerate(order_key, 1):
        lab.add(orders[k]["id"], f"O{i}")
        lab.add(orders[k]["client_request_id"], f"crid:O{i}")
    fills = _rows(conn, "SELECT id, order_id FROM fills ORDER BY id")
    for i, r in enumerate(sorted(fills, key=lambda r: (_ordinal(lab, r["order_id"]), r["id"])), 1):
        lab.fills[str(r["id"])] = f"F{i}"
    return lab


def _ordinal(lab: Labels, raw: Any) -> tuple[str, int]:
    label = lab.ids.get(str(raw), "~")
    head = label.rstrip("0123456789")
    tail = label[len(head):]
    return head, int(tail) if tail else 0


QUERIES = {
    "orders": "SELECT * FROM orders",
    "order_events": "SELECT * FROM order_events",
    "fills": "SELECT * FROM fills",
    "ledger": "SELECT * FROM ledger",
    "bets": "SELECT * FROM bets",
    "model_scores": "SELECT * FROM model_scores",
    "bankrolls": "SELECT * FROM bankrolls",
    "models": "SELECT * FROM models",
    "jobs": "SELECT * FROM jobs",
}
SORT = {
    "orders": lambda lab, r: _ordinal(lab, r["id"]),
    "order_events": lambda lab, r: (_ordinal(lab, r["order_id"]), r["id"]),
    "fills": lambda lab, r: (_ordinal(lab, r["order_id"]), r["id"]),
    "ledger": lambda lab, r: (lab.ids.get(str(r["bankroll_id"]), "~"), r["id"]),
    "bets": lambda lab, r: (_ordinal(lab, r["order_id"]), r["id"]),
    "model_scores": lambda lab, r: (lab.ids.get(str(r["model_id"]), "~"), r["game_id"], r["mode"]),
    "bankrolls": lambda lab, r: lab.ids.get(str(r["id"]), "~"),
    "models": lambda lab, r: _ordinal(lab, r["id"]),
    "jobs": lambda lab, r: lab.ids.get(str(r["id"]), "~"),
}


def normalize_row(table: str, row: dict[str, Any], lab: Labels) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in sorted(row.items()):
        if key in DROP.get(table, set()) or isinstance(value, (dt.datetime, dt.date)) or key.endswith("_at"):
            continue
        if key == "snapshot_id" and value is not None:
            out[key] = lab.snapshots.get(int(value), "S?")
        elif table == "ledger" and key == "ref_id" and row.get("ref_type") == "fill":
            out[key] = lab.fills.get(str(value), "F?")
        else:
            out[key] = lab.value(value)
    if table in ("models", "bankrolls", "orders"):
        out = {"label": lab.ids.get(str(row["id"]), "?"), **out}
    if table == "fills":  # the paper rule: a fill comes from a snapshot newer than the order's
        before = lab.order_snapshot.get(str(row["order_id"]))
        newer = None if before is None or row["snapshot_id"] is None else row["snapshot_id"] > before
        out = {"label": lab.fills.get(str(row["id"]), "F?"), **out, "after_order_snapshot": newer}
    return out


def dump(database_url: str) -> dict[str, list[dict[str, Any]]]:
    with db.connect(database_url) as conn:
        lab = build_labels(conn)
        result = {}
        for table in TABLES:
            rows = sorted(_rows(conn, QUERIES[table]), key=lambda r, t=table: SORT[t](lab, r))
            result[table] = [normalize_row(table, r, lab) for r in rows]
    return result


def compare(a: dict[str, list[dict[str, Any]]], b: dict[str, list[dict[str, Any]]], names: tuple[str, str]) -> list[str]:
    """Readable unified diff lines per table; empty when equal."""
    lines: list[str] = []
    for table in TABLES:
        left = [json.dumps(r, sort_keys=True) for r in a.get(table, [])]
        right = [json.dumps(r, sort_keys=True) for r in b.get(table, [])]
        if left != right:
            lines.append(f"--- {table}: {names[0]} {len(left)} rows, {names[1]} {len(right)} rows")
            lines += list(difflib.unified_diff(left, right, names[0], names[1], lineterm="", n=1))
    return lines

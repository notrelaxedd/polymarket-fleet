"""Human labels for the machine identifiers the pages show: roles, job kinds, job
statuses, job events, audit actions, robustness flags, model statuses, reason codes.

One table for the whole site, so `model_search` reads "Model Search" on the fleet
cards, the jobs list, the job page, the 3D page (GET /api/fleet `roles`) and the
fleet event feed alike. Only visible text goes through here: forms, data-* attributes,
URLs and JSON APIs keep the raw values.

`label(value, group=None)` looks the value up in the group's table (role, kind,
status, event, action, flag, model, source), then in the shared table, and turns any
other lowercase snake_case token into Title Case words
("lease_expired" -> "Lease Expired"). Jinja templates use it as the `label` filter:
`{{ job.kind | label('kind') }}`.
"""
from __future__ import annotations

import re
from typing import Any

ROLE_LABELS = {
    "idle": "Idle",
    "backtest": "Backtest",
    "model_search": "Model Search",
    "train": "Training",
    "trade": "Trading",
}
# The short names on the 3D page's buttons and machine list.
ROLE_SHORT = {"idle": "Idle", "backtest": "Backtest", "model_search": "Search", "train": "Train", "trade": "Trade"}

KIND_LABELS = {
    "sleep": "Test Sleep",
    "backtest": "Backtest",
    "validate": "Validation",
    "model_search": "Model Search",
    "train": "Training",
    "trade": "Trading",
}

JOB_STATUS_LABELS = {
    "queued": "Queued",
    "leased": "Running",
    "cancel_requested": "Cancelling",
    "succeeded": "Succeeded",
    "failed": "Failed",
    "cancelled": "Cancelled",
}

EVENT_LABELS = {
    "created": "Created",
    "targeted": "Sent to a Worker",
    "claimed": "Claimed",
    "released": "Handed Back",
    "lease_expired": "Lease Expired",
    "re-leased": "Lease Renewed",
    "re-offered": "Offered Again",
    "preempt_requested": "Asked to Stop",
    "cancel_requested": "Cancel Requested",
    "succeeded": "Succeeded",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "model_created": "Model Created",
    "model_exists": "Model Already Stored",
    "model_backtest": "Backtest Stored",
    "model_snapshot_backtest": "Snapshot Backtest Stored",
    "model_validation": "Validation Stored",
}

ACTION_LABELS = {
    "set_role": "Role Set",
    "auto_role": "Role Set for a Job",
    "auto_idle": "Back to Idle",
    "set_enabled": "Worker Enabled or Disabled",
    "kill": "Kill Switch On",
    "auto_kill": "Kill Switch On Automatically",
    "kill_reset": "Kill Switch Off",
    "live_off": "Live Trading Off",
}

FLAG_LABELS = {"overfit": "Overfit", "fragile": "Fragile", "regime_dependent": "Regime Dependent"}

MODEL_STATUS_LABELS = {
    "candidate": "Candidate",
    "paper_ok": "Paper OK",
    "live_eligible": "Live Eligible",
    "retired": "Retired",
}

SOURCE_LABELS = {
    "sim": "Sim",
    "polymarket_us": "Polymarket US",
    "polymarket_clob": "Polymarket CLOB",
    "closing_line": "Closing Line",
    "snapshots": "Snapshots",
    "espn": "ESPN",
    "yahoo": "Yahoo",
    "vegas_wp": "Vegas WP",
}

GROUPS: dict[str, dict[str, str]] = {
    "role": ROLE_LABELS,
    "kind": KIND_LABELS,
    "status": JOB_STATUS_LABELS,
    "event": EVENT_LABELS,
    "action": ACTION_LABELS,
    "flag": FLAG_LABELS,
    "model": MODEL_STATUS_LABELS,
    "source": SOURCE_LABELS,
}

# Without a group: statuses win over events (a job chip says "Cancelling"), roles over
# kinds ("train" is Training either way).
SHARED: dict[str, str] = {
    **SOURCE_LABELS, **FLAG_LABELS, **MODEL_STATUS_LABELS, **ACTION_LABELS, **EVENT_LABELS,
    **KIND_LABELS, **ROLE_LABELS, **JOB_STATUS_LABELS,
}

# Words the fallback writes in capitals or with their own spelling.
SPECIAL_WORDS = {
    "ok": "OK", "clv": "CLV", "roi": "ROI", "id": "ID", "api": "API", "url": "URL", "gtd": "GTD", "oom": "OOM",
    "wp": "WP", "espn": "ESPN", "clob": "CLOB", "us": "US", "pnl": "P&L", "cpu": "CPU", "ram": "RAM", "nfl": "NFL",
    "epa": "EPA", "qb": "QB", "pbp": "PBP", "ingame": "In-Game", "ssd": "SSD", "hdd": "HDD",
}
SMALL_WORDS = frozenset({"a", "an", "the", "and", "or", "of", "to", "in", "on", "by", "for", "at", "per", "vs"})
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_\- ]*")


def title_words(text: str) -> str:
    """"lease_expired" -> "Lease Expired", "rejected_by_exchange" -> "Rejected by Exchange",
    "cancel-all" -> "Cancel-All": underscores and spaces part the words, hyphens stay."""
    words = [w for w in re.split(r"[_\s]+", text) if w]
    out = []
    for i, word in enumerate(words):
        if i and word in SMALL_WORDS:
            out.append(word)
        else:
            out.append("-".join(SPECIAL_WORDS.get(part, part[:1].upper() + part[1:]) for part in word.split("-")))
    return " ".join(out)


def label(value: Any, group: str | None = None) -> str:
    """The human label of a machine identifier; "" for None. Text that is not a
    lowercase identifier (a name, a sentence with capitals) comes back unchanged."""
    if value is None:
        return ""
    text = str(value)
    table = GROUPS.get(group or "", {})
    if text in table:
        return table[text]
    if text in SHARED:
        return SHARED[text]
    return title_words(text) if IDENTIFIER.fullmatch(text) else text


def role_names(roles: tuple[str, ...] | list[str]) -> list[dict[str, str]]:
    """The roles in display order with their names, as GET /api/fleet `roles` lists them."""
    return [{"id": r, "name": label(r, "role"), "short": ROLE_SHORT.get(r, label(r, "role"))} for r in roles]

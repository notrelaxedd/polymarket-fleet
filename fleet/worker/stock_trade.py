"""Stock trade tick (contract sections 1 and 7): one decision per assignment per session.

Every stock_trade_tick_s, for each held stock_trade job: GET
/api/v1/stock_trade/state?job_id=...; nothing happens under kill, for an assignment
that is not active, or while decision.due is false. Otherwise the bar cache is
refreshed (fleet.worker.stock_cache; fetched again when its newest bar, or SPY's, the
models' reference series, is older than decision.bars_through, and the tick is skipped
if it still is) and fleet.worker.stock_plan turns the model's weights into whole-share
market-on-close orders (sizing, the host's limits, the backtest's rebalance-on-change
rule). The batch goes out once per session (POST /api/v1/stock_orders/request,
client_request_id = sha256(job_id|session_date|symbol|side)[:24], so a retry after a
lost answer is idempotent); an empty batch is posted too, so the host records the
decision. The names an order could not finish (cut by max_order or cash, or rejected
by the host for anything but max_position) are kept per job in memory and traded again
at the next session even when the weights did not change. release_jobs() is the POST
/api/v1/stock_trade/release handshake before a role change away from trade (bounded to
4 s, one retry), like fleet.worker.trade.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlencode

from fleet.common import http
from fleet.stocks.data import BENCHMARK, Bars
from fleet.worker import stock_cache
from fleet.worker.stock_plan import client_request_id, plan, plan_orders  # noqa: F401 (re-exported)

log = logging.getLogger("fleet.stock_trade")

STATE_PATH = "/api/v1/stock_trade/state"
REQUEST_PATH = "/api/v1/stock_orders/request"
RELEASE_PATH = "/api/v1/stock_trade/release"
RELEASE_TIMEOUT = 4.0
DEFAULT_SETTINGS: dict[str, Any] = {"stock_trade_tick_s": 30, "stock_decision_lead_min": 20}


def bars_end(bars: Bars, symbols: list[str]) -> str:
    """The newest bar date over symbols, or SPY's when that is older: SPY sets the
    rebalance anchors and the trend filter, so a decision never runs on a stale SPY."""
    dates = [bars[s][-1].date for s in symbols if bars.get(s)]
    newest = max(dates) if dates else ""
    spy = bars[BENCHMARK][-1].date if bars.get(BENCHMARK) else newest
    return min(newest, spy)


def refused(orders: list[dict[str, Any]], answer: dict[str, Any]) -> set[str]:
    """The names whose order the host rejected for a reason a later session can clear."""
    names = {o["client_request_id"]: o["symbol"] for o in orders}
    return {names[r["client_request_id"]] for r in answer.get("orders") or []
            if isinstance(r, dict) and r.get("client_request_id") in names
            and r.get("status") == "rejected" and r.get("reason") != "max_position"}


class StockTradeLoop:
    """The stock_trade jobs' tick, driven by the agent's main loop in the trade role."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.settings: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self.memo = stock_cache.BarsMemo()
        self.ticks = 0
        self.last_tick: dict[str, Any] | None = None
        self.jobs_seen: dict[str, dict[str, Any]] = {}
        self.posted: set[tuple[str, str]] = set()
        self.unfinished: dict[str, set[str]] = {}
        self._next_at = 0.0

    def held_jobs(self) -> list[dict[str, Any]]:
        return [j for j in getattr(self.agent, "trade_jobs", {}).values() if j.get("kind") == "stock_trade"]

    def tick_seconds(self) -> float:
        override = getattr(self.agent.options, "stock_trade_tick_s", None)
        value = override if override is not None else self.settings.get("stock_trade_tick_s")
        try:
            return max(0.05, float(value))
        except (TypeError, ValueError):
            return float(DEFAULT_SETTINGS["stock_trade_tick_s"])

    def maybe_run(self) -> bool:
        """Run a tick when one is due and a stock job is held; True when it ran."""
        if not self.held_jobs():
            return False
        now = self.agent._clock()
        if now < self._next_at:
            return False
        self._next_at = now + self.tick_seconds()
        self.run()
        return True

    def run(self) -> list[dict[str, Any]]:
        self.ticks += 1
        outcomes = [self.tick_job(job) for job in self.held_jobs()]
        self.last_tick = {"at": time.time(), "jobs": len(outcomes),
                          "posted": sum(1 for o in outcomes if o.get("action") == "posted")}
        return outcomes

    # one job

    def tick_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = str(job["id"])
        state = self.fetch_state(job)
        outcome = self._decide(job, state) if state is not None else {"action": "skip", "reason": "no state"}
        outcome["job_id"] = job_id
        self.jobs_seen[job_id] = {k: v for k, v in outcome.items() if k != "answer"}
        return outcome

    def _decide(self, job: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        if isinstance(state.get("settings"), dict):
            self.settings.update({k: v for k, v in state["settings"].items() if k in DEFAULT_SETTINGS})
        a, decision = state.get("assignment") or {}, state.get("decision") or {}
        session = str(decision.get("session_date") or "")
        if state.get("kill") or getattr(self.agent, "kill", False):
            return {"action": "skip", "reason": "kill"}
        if a.get("status") != "active":
            return {"action": "skip", "reason": f"assignment {a.get('status')}"}
        if not decision.get("due") or not session:
            return {"action": "skip", "reason": "not due"}
        if (str(job["id"]), session) in self.posted:
            return {"action": "skip", "reason": "already decided"}
        bars = self.ensure_bars(str(decision.get("bars_through") or ""), a.get("symbols") or [])
        if bars is None:
            return {"action": "skip", "reason": "bars behind"}
        try:
            batch = plan(state, bars, job["id"], unfinished=self.unfinished.get(str(job["id"])))
        except ValueError as exc:
            log.error("stock job %s: cannot decide (%s)", job["id"], exc)
            return {"action": "skip", "reason": str(exc)}
        orders = batch.orders
        answer = self.post_batch(job, a, session, orders)
        if answer is None:
            return {"action": "skip", "reason": "request not answered", "orders": len(orders)}
        self.posted.add((str(job["id"]), session))
        self.unfinished[str(job["id"])] = batch.unfinished | refused(orders, answer)
        return {"action": "posted", "session_date": session, "orders": len(orders), "answer": answer}

    def _get(self, path: str) -> Any:
        conf = self.agent.conf
        return http.get_json(conf["host_url"] + path, token=conf["worker_token"], timeout=self.agent.options.http_timeout)

    def _post(self, path: str, body: dict[str, Any], timeout: float | None = None) -> Any:
        conf = self.agent.conf
        return http.post_json(conf["host_url"] + path, body, token=conf["worker_token"],
                              timeout=timeout if timeout is not None else self.agent.options.http_timeout)

    def fetch_state(self, job: dict[str, Any]) -> dict[str, Any] | None:
        query = urlencode({"job_id": job["id"], "lease_token": job.get("lease_token") or ""})
        try:
            state = self._get(f"{STATE_PATH}?{query}")
            if not isinstance(state, dict):
                raise http.HttpConnectionError(f"bad stock trade state: {state!r}")
        except (http.HttpError, http.HttpConnectionError) as exc:
            log.warning("stock trade state for %s unavailable: %s", job["id"], exc)
            return None
        return state

    def ensure_bars(self, bars_through: str, symbols: list[str]) -> Bars | None:
        """The cached bars once they reach bars_through (two refreshes at most), else None."""
        conf = self.agent.conf
        timeout = max(self.agent.options.http_timeout, getattr(self.agent.options, "data_timeout", 15.0))
        for _ in (1, 2):
            try:
                path = stock_cache.refresh_stock_bars(conf["host_url"], conf["worker_token"], self.agent.state_dir, timeout)
                bars = self.memo.load(path)
            except (stock_cache.StockCacheError, OSError, ValueError) as exc:
                log.warning("stock bars unavailable: %s", exc)
                return None
            newest = bars_end(bars, symbols)
            if newest >= bars_through:
                return bars
        log.warning("stock bars end %s, the decision needs %s; skipping this tick", newest or "nowhere", bars_through)
        return None

    def post_batch(self, job: dict[str, Any], a: dict[str, Any], session: str,
                   orders: list[dict[str, Any]]) -> dict[str, Any] | None:
        body = {"job_id": str(job["id"]), "lease_token": job.get("lease_token"), "assignment_id": a.get("id"),
                "session_date": session, "orders": orders}
        try:
            answer = self._post(REQUEST_PATH, body)
            if not isinstance(answer, dict):
                raise http.HttpConnectionError(f"bad answer {answer!r}")
        except (http.HttpError, http.HttpConnectionError) as exc:
            log.warning("stock order batch for %s (%s) failed: %s", a.get("id"), session, exc)
            return None
        results = {o.get("client_request_id"): o for o in answer.get("orders") or [] if isinstance(o, dict)}
        for order in orders:
            got = results.get(order["client_request_id"]) or {}
            log.info("stock %s %s x%d @%s: %s (%s)", order["side"], order["symbol"], order["qty"],
                     order["ref_price_cents"], got.get("status"), got.get("reason") or order["rationale"])
        log.info("stock decision for assignment %s on %s: %d order(s)", a.get("id"), session, len(orders))
        return answer

    # handshake

    def release_jobs(self, jobs: list[dict[str, Any]]) -> dict[str, Any] | None:
        """POST /api/v1/stock_trade/release, bounded to 4 s, one retry; None when the
        host never answered for good (the agent then carries the releases)."""
        body = {"job_ids": [str(j["id"]) for j in jobs],
                "jobs": [{"id": str(j["id"]), "lease_token": str(j["lease_token"])} for j in jobs]}
        timeout = min(self.agent.options.http_timeout, RELEASE_TIMEOUT)
        for attempt in (1, 2):
            try:
                answer = self._post(RELEASE_PATH, body, timeout=timeout)
                if isinstance(answer, dict):
                    log.info("stock trade release: %s", {k: answer.get(k) for k in ("cancelled", "released")})
                    return answer
                raise http.HttpConnectionError(f"bad answer {answer!r}")
            except http.HttpError as exc:
                if exc.status < 500:
                    log.warning("stock trade release refused (%s); proceeding", exc)
                    return {"cancelled": 0, "released": []}
                log.warning("stock trade release failed (%s)%s", exc, "; retrying once" if attempt == 1 else "; proceeding")
            except http.HttpConnectionError as exc:
                log.warning("stock trade release not answered (%s)%s", exc, "; retrying once" if attempt == 1 else "; proceeding")
        return None

    def status(self) -> dict[str, Any]:
        """The status.json "stock_trade" block."""
        held = {str(j["id"]) for j in self.held_jobs()}
        return {"jobs": sorted(held), "tick_seconds": self.tick_seconds(), "ticks": self.ticks,
                "last_tick": self.last_tick, "outcomes": [self.jobs_seen[j] for j in sorted(held) if j in self.jobs_seen]}

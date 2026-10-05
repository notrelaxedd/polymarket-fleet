"""In-game fields of assignments (contract section 7, docs/TRADING.md "In-game trading
(step 6 Part C)"): ingame_model_id must be an ingame_wp model, trade_ingame defaults
from settings and is paper-only, the owner toggle is audited and cancels open in-game
orders, the in-game model is pinned once it has orders, and the kill cancels in-game
orders like any other."""
from __future__ import annotations

import itertools
from datetime import timedelta
from typing import Any

import pytest

from host import kill
from host.errors import BadRequest, Conflict
from host.trading import assignments, assignments_ingame, orders
from tests.conftest import insert_model, set_setting
from tests.test_exchange import KICKOFF, NOW, bankroll, make_assignment, make_game, make_market, make_model, make_order

GAME = "2026_05_KC_LV"
INGAME_PARAMS = {"l2": 1.0, "time_scale": 1.0, "fp_scale": 1.0}
_SEQ = itertools.count()


def ingame_model(conn, status: str = "candidate") -> dict[str, Any]:
    params = {**INGAME_PARAMS, "l2": 0.5 + next(_SEQ) / 100}  # distinct params: models are unique by params
    return insert_model(conn, family="ingame_wp", params=params, status=status,
                        artifact={"coef": [0.0] * 10}, metrics={"era": "search", "log_loss": 0.44})


def ingame_order(conn, assignment, market, price: float = 0.5, size: int = 4, status: str = "open") -> dict[str, Any]:
    order = make_order(conn, assignment, market, price, size, status=status)
    conn.execute("UPDATE orders SET ingame = true WHERE id = %s", (order["id"],))
    return order


def order_status(conn, order_id) -> str:
    return conn.execute("SELECT status FROM orders WHERE id = %s", (order_id,)).fetchone()["status"]


def audits(conn, action: str) -> list[dict[str, Any]]:
    return conn.execute("SELECT * FROM audit_log WHERE action = %s ORDER BY id", (action,)).fetchall()


def test_create_with_an_ingame_model_and_the_settings_default(conn):
    make_game(conn, GAME, "LV", "KC", KICKOFF)
    pre, wp = make_model(conn), ingame_model(conn)
    row = assignments.create_assignment(conn, GAME, pre["id"], "paper", 5_000, "owner", ingame_model_id=str(wp["id"]))
    assert row["ingame_model_id"] == wp["id"] and row["trade_ingame"] is False, "settings.trade_ingame defaults false"
    assert audits(conn, "assignment_created")[-1]["after"]["ingame_model_id"] == str(wp["id"])
    listed = assignments.list_assignments(conn, assignment_id=row["id"])[0]
    assert listed["ingame_model_id"] == wp["id"] and listed["trade_ingame"] is False
    set_setting(conn, "trade_ingame", True)
    other = assignments.create_assignment(conn, GAME, make_model(conn)["id"], "paper", 5_000, "owner", ingame_model_id=wp["id"])
    assert other["trade_ingame"] is True, "the default follows settings.trade_ingame"
    explicit = assignments.create_assignment(conn, GAME, make_model(conn)["id"], "paper", 5_000, "owner", trade_ingame=False)
    assert explicit["trade_ingame"] is False and explicit["ingame_model_id"] is None


def test_create_refuses_bad_ingame_models(conn):
    make_game(conn, GAME, "LV", "KC", KICKOFF)
    pre, wp = make_model(conn), ingame_model(conn)
    for bad, message in ((pre["id"], "must be an ingame_wp model"), ("nope", "unknown in-game model"),
                         ("00000000-0000-0000-0000-000000000000", "unknown in-game model")):
        with pytest.raises(BadRequest, match=message):
            assignments.create_assignment(conn, GAME, pre["id"], "paper", 5_000, "owner", ingame_model_id=bad)
    with pytest.raises(BadRequest, match="trades only in-game"):
        assignments.create_assignment(conn, GAME, wp["id"], "paper", 5_000, "owner")
    retired = ingame_model(conn, status="retired")
    with pytest.raises(BadRequest, match="retired"):
        assignments.create_assignment(conn, GAME, pre["id"], "paper", 5_000, "owner", ingame_model_id=retired["id"])
    with pytest.raises(BadRequest, match="true or false"):
        assignments.create_assignment(conn, GAME, pre["id"], "paper", 5_000, "owner", trade_ingame="yes")
    assert conn.execute("SELECT count(*) AS n FROM assignments").fetchone()["n"] == 0


def test_live_assignments_never_trade_ingame(conn):
    set_setting(conn, "trade_ingame", True)
    assert assignments_ingame.resolve_new(conn, "live", None, None) == (None, False), "the default is false for live"
    with pytest.raises(Conflict, match="paper-only"):
        assignments_ingame.resolve_new(conn, "live", None, True)
    make_game(conn, GAME, "LV", "KC", KICKOFF)
    live = make_assignment(conn, GAME, make_model(conn, status="live_eligible"), mode="live")
    wp = ingame_model(conn)
    row = assignments_ingame.set_ingame(conn, live["id"], "owner", {"ingame_model_id": str(wp["id"])})
    assert row["ingame_model_id"] == wp["id"] and row["trade_ingame"] is False
    with pytest.raises(Conflict, match="paper-only"):
        assignments_ingame.set_ingame(conn, live["id"], "owner", {"trade_ingame": True})


def test_toggle_sets_fields_audits_and_cancels_open_ingame_orders(conn):
    make_game(conn, GAME, "LV", "KC", NOW - timedelta(minutes=30))
    home = make_market(conn, GAME, "home")
    a = make_assignment(conn, GAME, make_model(conn), bankroll_cents=10_000)
    wp = ingame_model(conn)
    on = assignments_ingame.set_ingame(conn, a["id"], "owner", {"ingame_model_id": str(wp["id"]), "trade_ingame": True})
    assert (on["ingame_model_id"], on["trade_ingame"], on["orders_cancelled"]) == (wp["id"], True, 0)
    pre_order = make_order(conn, a, home, 0.45, 5, status="open")
    in_order = ingame_order(conn, a, home, 0.50, 4)
    reserved = bankroll(conn, a)["reserved_cents"]
    with pytest.raises(Conflict, match="in-game orders"):
        assignments_ingame.set_ingame(conn, a["id"], "owner", {"ingame_model_id": str(ingame_model(conn)["id"])})
    with pytest.raises(Conflict, match="in-game orders"):
        assignments_ingame.set_ingame(conn, a["id"], "owner", {"ingame_model_id": None})
    off = assignments_ingame.set_ingame(conn, a["id"], "owner", {"trade_ingame": False})
    assert off["trade_ingame"] is False and off["ingame_model_id"] == wp["id"] and off["orders_cancelled"] == 1
    assert order_status(conn, in_order["id"]) == "cancelled" and order_status(conn, pre_order["id"]) == "open"
    assert bankroll(conn, a)["reserved_cents"] == reserved - in_order["cost_cents"], "the in-game reservation is released"
    rows = audits(conn, "assignment_ingame")
    assert [(r["before"], r["after"]) for r in rows] == [
        ({"ingame_model_id": None, "trade_ingame": False},
         {"ingame_model_id": str(wp["id"]), "trade_ingame": True, "orders_cancelled": 0}),
        ({"ingame_model_id": str(wp["id"]), "trade_ingame": True},
         {"ingame_model_id": str(wp["id"]), "trade_ingame": False, "orders_cancelled": 1}),
    ]
    assert rows[0]["actor"] == "owner"
    filled = ingame_order(conn, a, home, 0.50, 2)
    orders.record_fill(conn, filled["id"], 0.50, 2, 1, "paper", "test")
    with pytest.raises(Conflict, match="in-game orders"):
        assignments_ingame.set_ingame(conn, a["id"], "owner", {"ingame_model_id": None})


def test_toggle_refusals(conn):
    make_game(conn, GAME, "LV", "KC", KICKOFF)
    a = make_assignment(conn, GAME, make_model(conn))
    with pytest.raises(BadRequest, match="give ingame_model_id"):
        assignments_ingame.set_ingame(conn, a["id"], "owner", {})
    with pytest.raises(BadRequest, match="must be an ingame_wp model"):
        assignments_ingame.set_ingame(conn, a["id"], "owner", {"ingame_model_id": str(make_model(conn)["id"])})
    with pytest.raises(BadRequest, match="true or false"):
        assignments_ingame.set_ingame(conn, a["id"], "owner", {"trade_ingame": 1})
    conn.execute("UPDATE assignments SET status = 'settled' WHERE id = %s", (a["id"],))
    with pytest.raises(Conflict, match="assignment is settled"):
        assignments_ingame.set_ingame(conn, a["id"], "owner", {"trade_ingame": True})
    assert audits(conn, "assignment_ingame") == []


def test_model_change_is_free_without_ingame_orders(conn):
    make_game(conn, GAME, "LV", "KC", KICKOFF)
    home = make_market(conn, GAME, "home")
    a = make_assignment(conn, GAME, make_model(conn))
    first, second = ingame_model(conn), ingame_model(conn)
    assignments_ingame.set_ingame(conn, a["id"], "owner", {"ingame_model_id": str(first["id"])})
    make_order(conn, a, home, 0.45, 5, status="open")  # a pre-game order does not pin the in-game model
    row = assignments_ingame.set_ingame(conn, a["id"], "owner", {"ingame_model_id": str(second["id"])})
    assert row["ingame_model_id"] == second["id"]
    rejected = ingame_order(conn, a, home, status="approved")
    conn.execute("UPDATE orders SET status = 'rejected' WHERE id = %s", (rejected["id"],))
    cleared = assignments_ingame.set_ingame(conn, a["id"], "owner", {"ingame_model_id": None})
    assert cleared["ingame_model_id"] is None, "a rejected in-game order never pins the model"


def test_kill_cancels_ingame_orders(conn):
    make_game(conn, GAME, "LV", "KC", NOW - timedelta(minutes=30))
    home = make_market(conn, GAME, "home")
    a = make_assignment(conn, GAME, make_model(conn), bankroll_cents=10_000)
    assignments_ingame.set_ingame(conn, a["id"], "owner", {"ingame_model_id": str(ingame_model(conn)["id"]), "trade_ingame": True})
    first, second = ingame_order(conn, a, home, 0.50, 4), ingame_order(conn, a, home, 0.52, 3, status="approved")
    assert bankroll(conn, a)["reserved_cents"] > 0
    assert kill.set_kill(conn, "owner") is True
    assert order_status(conn, first["id"]) == "cancelled" and order_status(conn, second["id"]) == "cancelled"
    assert bankroll(conn, a)["reserved_cents"] == 0
    after = conn.execute("SELECT status, trade_ingame FROM assignments WHERE id = %s", (a["id"],)).fetchone()
    assert after["status"] == "halted" and after["trade_ingame"] is True, "the kill halts and cancels, nothing else"


def test_api_create_and_toggle(client, conn):
    make_game(conn, GAME, "LV", "KC", KICKOFF)
    pre, wp = make_model(conn), ingame_model(conn)
    r = client.post("/api/assignments", json={"game_id": GAME, "model_id": str(pre["id"]), "ingame_model_id": str(wp["id"]),
                                              "trade_ingame": True})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["ingame_model_id"] == str(wp["id"]) and body["trade_ingame"] is True
    bad = client.post("/api/assignments", json={"game_id": GAME, "model_id": str(make_model(conn)["id"]),
                                                "ingame_model_id": str(pre["id"])})
    assert bad.status_code == 400 and "ingame_wp" in bad.json()["detail"]
    listed = client.get("/api/assignments").json()[0]
    assert listed["ingame_model_id"] == str(wp["id"]) and listed["trade_ingame"] is True
    r = client.post(f"/api/assignments/{body['id']}/ingame", json={"trade_ingame": False})
    assert r.status_code == 200 and r.json()["trade_ingame"] is False and r.json()["ingame_model_id"] == str(wp["id"])
    r = client.post(f"/api/assignments/{body['id']}/ingame", json={"ingame_model_id": None})
    assert r.status_code == 200 and r.json()["ingame_model_id"] is None, "an explicit null clears the model"
    assert client.post(f"/api/assignments/{body['id']}/ingame", json={}).status_code == 400
    assert client.post(f"/api/assignments/{body['id']}/ingame", json={"trade_ingame": None}).status_code == 400
    assert client.post("/api/assignments/nope/ingame", json={"trade_ingame": True}).status_code == 404
    assert [r["actor"] for r in audits(conn, "assignment_ingame")] == ["dev", "dev"]

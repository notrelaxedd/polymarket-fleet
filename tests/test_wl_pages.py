"""The workloads pages (docs/workloads-design.md section 10): /machines, /workloads,
/workloads/{name}, /outbound, the machine logs and the typed unpin page, plus the sub-nav
on /fleet. Page tests insert rows with SQL (tests/hw/seed_workloads.py) and need none of
the other workloads modules. The form tests call the contract functions through the form
handlers, so they pass once the host modules are merged (the `merged` marker below)."""
from __future__ import annotations

import dataclasses
import sys
import types
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from host.api.app import create_app
from tests.conftest import flash_cookie
from tests.hw import seed_workloads as sw
from tests.pagecheck import page

EM_DASH = chr(0x2014)
PAGES = ["/machines", "/workloads", "/outbound", "/fleet/list", "/workloads/hello", "/workloads/demo-site"]


@pytest.fixture(autouse=True)
def placement(monkeypatch):
    """The host's check_placement when it exists; until it does, a stand-in with the same
    signature that knows the RAM rule only (the real one is covered by its own tests)."""
    try:
        import host.workloads.placement  # noqa: F401
        return
    except ImportError:
        pass

    @dataclass(frozen=True)
    class Refusal:
        code: str
        message: str

    def check_placement(manifest, machine, *, image_size_mb=None, image_published=True, workload_enabled=True):
        need, have = manifest.resources.min_ram_mb, machine["ram_total_mb"]
        return [Refusal("ram_too_small", f"needs {need} MB RAM, machine has {have} MB")] if have is None or have < need else []

    module = types.ModuleType("host.workloads.placement")
    module.check_placement, module.Refusal = check_placement, Refusal
    monkeypatch.setitem(sys.modules, "host.workloads.placement", module)


@pytest.fixture
def seeded(conn):
    return sw.seed_conn(conn)


def get(client, path, status=200):
    r = client.get(path)
    assert r.status_code == status, (path, r.status_code, r.text[:300])
    return page(r.text)


def subnav(doc):
    return {a.attr("data-subnav"): a for a in doc.select("[data-subnav]")}


# ------------------------------------------------------------------ rendering


def test_pages_render_empty(client):
    for path, h1 in [("/machines", "Machines"), ("/workloads", "Workloads"), ("/outbound", "Approvals")]:
        doc = get(client, path)
        assert doc.one("h1").text == h1 and doc.page_name == "fleet", "the Fleet nav item stays current"
        assert 2 <= doc.count("main .stat") <= 4 and doc.has("details.intro")
        assert doc.nav("fleet").is_current and not doc.nav("jobs").is_current
    assert "No machines yet" in get(client, "/machines").text
    assert "No workloads yet" in get(client, "/workloads").text
    assert "Nothing is waiting" in get(client, "/outbound").text


def test_pages_render_seeded_without_em_dash(client, seeded):
    for path in PAGES + [f"/machines/{seeded['pi1']}/logs", f"/machines/{seeded['pi2']}/unpin"]:
        r = client.get(path)
        assert r.status_code == 200 and EM_DASH not in r.text, path
        assert r.headers["cache-control"] == "no-store"


def test_subnav_on_fleet_and_every_new_page(client, conn, seeded):
    sw.add_outbound(conn, "hello", "log", {"message": "again"})
    expected = {"/fleet/list": "workers", "/machines": "machines", "/workloads": "workloads", "/workloads/hello": "workloads",
                "/outbound": "approvals", f"/machines/{seeded['pi1']}/logs": "machines"}
    for path, current in expected.items():
        doc = get(client, path)
        links = subnav(doc)
        assert list(links) == ["workers", "machines", "workloads", "approvals"], path
        assert [k for k, a in links.items() if a.is_current] == [current], path
        assert [a.target for a in links.values()] == ["/fleet/list", "/machines", "/workloads", "/outbound"]
        assert links["approvals"].one(".count").attr("data-count") == "3" and "pending" in links["approvals"].text, path
        assert doc.nav("fleet").is_current and len(doc.select("[data-nav]")) == 6, "five sections plus the wordmark"


def test_machine_grid(client, seeded):
    doc = get(client, "/machines")
    assert doc.row_ids("machine") == [seeded[k] for k in ("mini", "native", "old", "pi1", "pi2")], "sorted by name"
    pi1 = doc.row("machine", seeded["pi1"])
    assert pi1.chip("disk").text == "flash 16 GB" and pi1.chip("ram").text == "4 GB RAM" and pi1.chip("state").text == "Running"
    assert pi1.one(".dot").attr("aria-label") == "online" and not pi1.has('[data-chip="offline"]')
    select = pi1.one("select[name=workload]")
    assert select.attr("data-autosubmit") == "1" and not select.disabled and pi1.one("form.machine-assign").target == f"/machines/{seeded['pi1']}/assign"
    assert [o.attr("value") for o in select.select("option")] == ["none", "archive", "demo-site", "hello", "polymarket"]
    assert [o.attr("value") for o in select.select("option[selected]")] == ["hello"]
    assert pi1.has('[data-action="logs"]') and pi1.has('[data-action="pin"]') and pi1.has('[data-action="disk-type"]')
    assert pi1.has('[data-action="details"]') and pi1.action("disable").has('input[name="enabled"][value="false"]')
    old = doc.row("machine", seeded["old"])
    assert old.one(".dot").attr("aria-label") == "offline" and old.chip("offline").text == "Offline" and old.chip("disk").text == "hdd 488 GB"
    assert doc.row("machine", seeded["mini"]).chip("state").text == "Starting"
    assert doc.stat("online").prop("Machines online") == "4 / 5" and doc.stat("pinned").prop("Pinned") == "1"


def test_online_follows_the_online_after_setting(client, conn, seeded):
    conn.execute("UPDATE machines SET last_heartbeat_at = now() - interval '40 seconds' WHERE id = %s", (seeded["pi1"],))
    assert get(client, "/machines").row("machine", seeded["pi1"]).one(".dot").attr("aria-label") == "stale"
    conn.execute("UPDATE settings SET value = '60'::jsonb WHERE key = 'online_after_seconds'")
    assert get(client, "/machines").row("machine", seeded["pi1"]).one(".dot").attr("aria-label") == "online"


def test_disk_type_override_shows_in_the_chip(client, conn, seeded):
    conn.execute("UPDATE machines SET disk_type_override = 'ssd' WHERE id = %s", (seeded["pi1"],))
    row = get(client, "/machines").row("machine", seeded["pi1"])
    assert row.chip("disk").text == "ssd 16 GB"
    assert [o.attr("value") for o in row.select('form[data-form="disk-type"] option[selected]')] == ["ssd"]


def test_refused_options_are_disabled_with_the_reason(client, seeded):
    doc = get(client, "/machines")
    options = {o.attr("value"): o for o in doc.row("machine", seeded["pi1"]).select("select[name=workload] option")}
    assert options["demo-site"].disabled and options["demo-site"].text.startswith("demo-site (") and "8192" in options["demo-site"].text
    assert not options["hello"].disabled and not options["none"].disabled and options["hello"].text == "hello"
    big = {o.attr("value"): o for o in doc.row("machine", seeded["mini"]).select("select[name=workload] option")}
    assert not big["demo-site"].disabled and big["demo-site"].has_attr("selected") and big["demo-site"].text == "demo-site"


def test_pinned_machine_is_locked_with_an_unpin_item(client, seeded):
    row = get(client, "/machines").row("machine", seeded["pi2"])
    assert row.one("select[name=workload]").disabled and row.one("form.machine-assign button").disabled
    assert row.chip("pinned").text == "pinned: live trading"
    assert row.action("unpin").target == f"/machines/{seeded['pi2']}/unpin" and not row.has('[data-action="pin"]')


def test_native_worker_machine_is_locked(client, seeded):
    row = get(client, "/machines").row("machine", seeded["native"])
    assert row.one("select[name=workload]").disabled and row.chip("native").text == "native worker running"


def test_unpin_confirm_page(client, seeded):
    doc = get(client, f"/machines/{seeded['pi2']}/unpin")
    assert doc.one("[data-phrase]").text == "UNPIN pi-2" and doc.form("unpin").target == f"/machines/{seeded['pi2']}/unpin"
    assert doc.input("confirm").attr("value") == "" and doc.has(".stat")
    assert "is not pinned" in get(client, f"/machines/{seeded['pi1']}/unpin").text
    get(client, "/machines/m_nope00/unpin", 404)
    get(client, "/machines/m_nope00/logs", 404)


def test_machine_logs_newest_last_and_capped(client, conn, seeded):
    sw.add_logs(conn, seeded["pi1"], "hello", 250, prefix="more")
    doc = get(client, f"/machines/{seeded['pi1']}/logs")
    lines = doc.one("pre.log").text
    assert doc.stat("lines").prop("Lines shown") == "200" and lines.endswith("more 250") and "stdout more 51 " in lines and "stdout more 50 " not in lines
    assert get(client, f"/machines/{seeded['old']}/logs").has(".empty")


# ------------------------------------------------------------------ workloads


def test_workload_rows(client, seeded):
    doc = get(client, "/workloads")
    assert doc.row_ids("workload") == ["archive", "demo-site", "hello", "polymarket"]
    assert doc.row("workload", "archive").chip("image").text == "no image"
    hello = doc.row("workload", "hello")
    assert hello.chip("image").text == "image published" and "1 running" in hello.text and "1 queued" in hello.text
    assert hello.action("disable").has('input[value="false"]') and hello.one("a.row-main").target == "/workloads/hello"
    assert doc.has('[data-form="sync"]') and doc.stat("workloads").prop("Workloads enabled") == "4 / 4"


def test_workload_detail(client, seeded):
    doc = get(client, "/workloads/hello")
    assert doc.one("h1").text == "hello" and doc.card("image").chip("image").text == "published"
    assert sw.DIGEST[:19] in doc.card("image").text
    assert doc.card("machines").row_ids("machine") == [seeded["pi1"]] and doc.card("machines").chip("state").text == "Running"
    jobs = doc.card("jobs-running")
    assert [r.chip_texts()[0] for r in jobs.rows("job")] == ["Running", "Queued"] and jobs.one(".bar").attr("aria-valuenow") == "40"
    assert jobs.row("job", seeded["job"]).action("cancel").target.endswith(f"/jobs/{seeded['job']}/cancel")
    assert sorted(r.chip_texts()[0] for r in doc.card("jobs-done").rows("job")) == ["Failed", "Succeeded"]
    form = doc.form("send-job")
    assert form.target == "/workloads/hello/jobs" and [o.attr("value") for o in form.select("select[name=kind] option")] == ["hello"]
    assert {o.attr("value") for o in form.select("select[name=target] option")} >= {"any", seeded["pi1"]}
    log = doc.card("logs").one("pre.log").text
    assert log.endswith("line 60") and "stdout line 11 " in log and "stdout line 10 " not in log and log.count("stdout line") == 50
    assert "Mode" in doc.card("manifest").text and "Jobs: Hello" in doc.card("manifest").text
    assert doc.card("outbound").row_ids("outbound") and doc.stat("approvals").text.startswith("1")


def test_service_workload_has_no_send_job_form(client, seeded):
    doc = get(client, "/workloads/polymarket")
    assert not doc.has('[data-form="send-job"]') and doc.card("machines").row_ids("machine") == [seeded["pi2"]]
    get(client, "/workloads/nope", 404)
    get(client, "/workloads/Bad_Name", 404)


def test_secrets_are_write_only(client, seeded):
    doc = get(client, "/workloads/demo-site")
    rows = {r.attr("data-id"): r for r in doc.card("secrets").rows("secret")}
    assert list(rows) == ["SMTP_URL", "EMAIL_FROM"], "declared names, set or not"
    assert rows["SMTP_URL"].chip("set").text == "set" and rows["SMTP_URL"].chip("scope").text == "host only"
    assert rows["EMAIL_FROM"].chip("set").text == "not set"
    form = rows["SMTP_URL"].form("secret")
    assert form.target == "/workloads/demo-site/secrets" and form.input("value").attr("type") == "password" and form.input("value").attr("value") is None
    assert rows["SMTP_URL"].action("delete-secret").has('input[name="delete"][value="1"]') and not rows["EMAIL_FROM"].has('[data-action="delete-secret"]')


def test_secret_values_never_appear_in_any_page(client, conn, seeded):
    conn.execute("UPDATE workload_secrets SET ciphertext = %s WHERE name = 'SMTP_URL'", (b"smtps://user:hunter2@mail.example:465",))
    for path in PAGES + ["/machines/" + seeded["mini"] + "/logs"]:
        html = client.get(path).text
        assert "hunter2" not in html and "smtps://" not in html and "737478" not in html, path
        assert "ciphertext" not in html and "nonce" not in html, path


def test_outbound_page(client, conn, seeded):
    doc = get(client, "/outbound")
    assert doc.stat("pending").prop("Waiting for you") == "2" and doc.stat("sent").prop("Sent") == "1"
    assert doc.card("done").count('[data-row="outbound-done"]') == 3
    row = doc.row("outbound", seeded["mail"])
    assert row.one(".row-title").text == "email to client@example.com: Your demo site is ready for review"
    assert row.action("approve").target == f"/outbound/{seeded['mail']}/approve" and row.action("reject").target == f"/outbound/{seeded['mail']}/reject"
    assert row.one("details.disclosure").attr("data-key") == f"outbound-{seeded['mail']}"
    assert '"subject": "Your demo site is ready for review"' in row.one("pre.payload").text and "demo-site" in row.text
    done = {r.attr("data-id"): r for r in doc.rows("outbound-done")}
    assert sorted(r.chip_texts()[0] for r in done.values()) == ["Failed", "Rejected", "Sent"]
    assert any("550 mailbox full" in r.text for r in done.values())


def test_outbound_payload_is_escaped(client, conn):
    sw.add_workload(conn, "mailer", actions=("email",))
    sw.add_outbound(conn, "mailer", "email", {"to": "<img src=x>", "subject": "<script>alert(1)</script>"})
    html = client.get("/outbound").text
    assert "<script>alert" not in html and "<img src=x>" not in html and "&lt;script&gt;" in html


# ------------------------------------------------------------------ auth


def test_owner_auth_is_enforced(config, client, seeded):
    strict = dataclasses.replace(config, dev=False, owner_login="owner@example.com")
    owner = {"Tailscale-User-Login": "owner@example.com"}
    with TestClient(create_app(strict)) as c:
        for path in PAGES + [f"/machines/{seeded['pi2']}/unpin", f"/machines/{seeded['pi1']}/logs"]:
            assert c.get(path).status_code == 401, path
            assert c.get(path, headers={"Tailscale-User-Login": "intruder@example.com"}).status_code == 401, path
            assert c.get(path, headers=owner).status_code == 200, path
        for path in ("/machine-enroll-token", "/workloads/sync", f"/machines/{seeded['pi1']}/assign", "/outbound/x/approve"):
            assert c.post(path, headers={"Tailscale-User-Login": "intruder@example.com"}).status_code == 401, path
        r = c.post(f"/machines/{seeded['pi1']}/enabled", data={"enabled": "false"}, headers={**owner, "Origin": "http://evil.example"})
        assert r.status_code == 403


# ------------------------------------------------------------------ forms that need no other module


def test_machine_enabled_and_disk_type_forms(client, conn, seeded):
    m = seeded["pi1"]
    r = client.post(f"/machines/{m}/enabled", data={"enabled": "false"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/machines" and flash_cookie(r) == "pi-1 disabled"
    assert conn.execute("SELECT enabled FROM machines WHERE id = %s", (m,)).fetchone()["enabled"] is False
    assert get(client, "/machines").row("machine", m).chip("disabled").text == "Disabled"
    r = client.post(f"/machines/{m}/disk-type", data={"disk_type": "hdd"}, follow_redirects=False)
    assert r.status_code == 303 and conn.execute("SELECT disk_type_override FROM machines WHERE id = %s", (m,)).fetchone()["disk_type_override"] == "hdd"
    client.post(f"/machines/{m}/disk-type", data={"disk_type": "auto"})
    assert conn.execute("SELECT disk_type_override FROM machines WHERE id = %s", (m,)).fetchone()["disk_type_override"] is None
    r = client.post(f"/machines/{m}/disk-type", data={"disk_type": "tape"})
    assert r.status_code == 400 and "disk type must be one of" in page(r.text).one("[data-error]").text
    actions = [x["action"] for x in conn.execute("SELECT action FROM audit_log ORDER BY id").fetchall()]
    assert actions == ["machine_enabled", "machine_disk_type", "machine_disk_type"]
    assert client.post("/machines/m_nope00/enabled", data={"enabled": "true"}).status_code == 404


def test_workload_enabled_form(client, conn, seeded):
    r = client.post("/workloads/hello/enabled", data={"enabled": "false"}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == "hello disabled"
    assert get(client, "/workloads").row("workload", "hello").chip("disabled").text == "disabled"
    assert client.post("/workloads/nope/enabled", data={"enabled": "true"}).status_code == 404


# ------------------------------------------------------------------ forms over the host contract (verified only after the merge)


def test_assign_form_posts_and_redirects(client, conn, seeded):
    r = client.post(f"/machines/{seeded['old']}/assign", data={"workload": "hello"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/machines"
    assert flash_cookie(r) == "old-box: assigned hello (old-box is offline; the change takes effect when it comes back)"
    row = conn.execute("SELECT workload, state FROM workload_assignments WHERE machine_id = %s", (seeded["old"],)).fetchone()
    assert row["workload"] == "hello" and row["state"] == "pending"
    client.post(f"/machines/{seeded['old']}/assign", data={"workload": "none"})
    assert conn.execute("SELECT workload FROM workload_assignments WHERE machine_id = %s", (seeded["old"],)).fetchone()["workload"] is None


def test_assign_refusals_rerender_with_the_error_and_change_nothing(client, conn, seeded):
    before = conn.execute("SELECT machine_id, workload, epoch FROM workload_assignments ORDER BY machine_id").fetchall()
    r = client.post(f"/machines/{seeded['pi2']}/assign", data={"workload": "hello"})
    assert r.status_code == 409 and "pinned" in page(r.text).one("[data-error]").text
    r = client.post(f"/machines/{seeded['pi1']}/assign", data={"workload": "demo-site"})
    assert r.status_code == 422 and "placement refused" in page(r.text).one("[data-error]").text
    r = client.post(f"/machines/{seeded['native']}/assign", data={"workload": "hello"})
    assert r.status_code == 409 and "native" in page(r.text).one("[data-error]").text
    assert conn.execute("SELECT machine_id, workload, epoch FROM workload_assignments ORDER BY machine_id").fetchall() == before


def test_pin_and_typed_unpin_forms(client, conn, seeded):
    m = seeded["pi1"]
    r = client.post(f"/machines/{m}/pin", data={"reason": "demo day"}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == "pi-1: pinned"
    assert get(client, "/machines").row("machine", m).chip("pinned").text == "pinned: demo day"
    r = client.post(f"/machines/{m}/unpin", data={"confirm": "unpin pi-1"})
    assert r.status_code == 400 and page(r.text).one("[data-error]").text and page(r.text).input("confirm").attr("value") == "unpin pi-1"
    assert conn.execute("SELECT pinned FROM machines WHERE id = %s", (m,)).fetchone()["pinned"] is True
    r = client.post(f"/machines/{m}/unpin", data={"confirm": "UNPIN pi-1"}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == "pi-1: unpinned"
    assert conn.execute("SELECT pinned FROM machines WHERE id = %s", (m,)).fetchone()["pinned"] is False


def test_secret_forms_are_write_only(client, conn, seeded, monkeypatch):
    import base64
    monkeypatch.setenv("FLEET_SECRETS_KEY", base64.b64encode(bytes(range(32))).decode())
    r = client.post("/workloads/hello/secrets", data={"name": "HELLO_GREETING", "value": "hunter2-value"}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == "HELLO_GREETING saved" and "hunter2-value" not in r.text
    assert "hunter2-value" not in client.get("/workloads/hello").text
    r = client.post("/workloads/hello/secrets", data={"name": "NOT_DECLARED", "value": "zzz-secret"})
    assert r.status_code == 400 and "zzz-secret" not in r.text and page(r.text).one("[data-error]").text
    assert client.post("/workloads/hello/secrets", data={"name": "HELLO_GREETING", "value": ""}).status_code == 400
    r = client.post("/workloads/hello/secrets", data={"name": "HELLO_GREETING", "delete": "1"}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == "HELLO_GREETING deleted"
    assert conn.execute("SELECT count(*) AS n FROM workload_secrets WHERE workload = 'hello'").fetchone()["n"] == 0


def test_send_job_and_cancel_forms(client, conn, seeded):
    r = client.post("/workloads/hello/jobs", data={"kind": "hello", "target": "any", "params": '{"name": "Ada"}'}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r).endswith("job " + flash_cookie(r).split()[2] + " queued")
    job = conn.execute("SELECT id, params, status FROM workload_jobs WHERE workload = 'hello' ORDER BY created_at DESC LIMIT 1").fetchone()
    assert job["params"] == {"name": "Ada"} and job["status"] == "queued"
    for body, text in [({"kind": "hello", "params": "{nope"}, "not valid JSON"), ({"kind": "hello", "params": "[1]"}, "JSON object"),
                       ({"kind": "other", "params": "{}"}, "kind")]:
        r = client.post("/workloads/hello/jobs", data=body)
        doc = page(r.text)
        assert r.status_code == 400 and text in doc.one("[data-error]").text and doc.card("send").is_open
    r = client.post(f"/workloads/hello/jobs/{job['id']}/cancel", follow_redirects=False)
    assert r.status_code == 303 and conn.execute("SELECT status FROM workload_jobs WHERE id = %s", (job["id"],)).fetchone()["status"] == "cancelled"


def test_outbound_forms(client, conn, seeded):
    r = client.post(f"/outbound/{seeded['mail']}/approve", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/outbound"
    assert conn.execute("SELECT status, decided_by FROM outbound_actions WHERE id = %s", (seeded["mail"],)).fetchone()["status"] == "approved"
    r = client.post(f"/outbound/{seeded['mail']}/approve")
    assert r.status_code == 409 and page(r.text).one("[data-error]").text
    other = conn.execute("SELECT id FROM outbound_actions WHERE status = 'pending'").fetchone()["id"]
    r = client.post(f"/outbound/{other}/reject", data={"reason": "not now"}, follow_redirects=False)
    assert r.status_code == 303 and conn.execute("SELECT status FROM outbound_actions WHERE id = %s", (other,)).fetchone()["status"] == "rejected"


def test_enroll_token_page_shows_the_token_once(client, conn):
    r = client.post("/machine-enroll-token")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    doc = page(r.text)
    token = doc.one("#token").text
    assert len(token) >= 20 and "ts.net" not in token
    base = "http://127.0.0.1:8080"
    assert doc.one("[data-command]").text == f"curl -fsSL {base}/install-agent.sh | sudo bash -s -- {base} {token}"
    assert token not in client.get("/machines").text
    assert conn.execute("SELECT count(*) AS n FROM machine_enroll_tokens").fetchone()["n"] == 1


def test_sync_form(client, conn, monkeypatch, tmp_path):
    folder = tmp_path / "wl" / "hi"
    folder.mkdir(parents=True)
    (folder / "workload.toml").write_text(
        'schema = 1\nname = "hi"\nimage = "fleet/hi"\n[resources]\nmin_ram_mb = 128\n[runtime]\nmode = "jobs"\njob_kinds = ["hi"]\n')
    monkeypatch.setenv("FLEET_WORKLOADS_DIR", str(tmp_path / "wl"))
    r = client.post("/workloads/sync", follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == "synced 1 workloads"
    assert get(client, "/workloads").row_ids("workload") == ["hi"]

"""Paper parity: Polymarket as a workload trades exactly like the native worker.

    sudo PYTHONPATH=$PWD python tools/workloads/paper_parity.py \
        [--main-checkout PATH] [--image polymarket-builder/polymarket:dev] [--modes native,container]

Each mode gets a fresh Postgres database (from FLEET_TEST_DATABASE_URL, the admin URL),
its own host app (uvicorn on 127.0.0.1:<free port>, FLEET_DEV=1, the host loop) and the
real exchange loop in this process on the sim source with a FROZEN clock, so every
snapshot of a market has the same book. Then the same scenario (parity/scenario.py):
fixture games plus three future paper games, a root elo_blend model, a `train` job run
by the worker, three paper assignments traded until the worker goes quiet (at least
--ticks ticks, then --quiet ticks in a row with no proposal and no open order), the
games settled with fixed scores. The worker is:
- native: main's fleet/ (a checkout of main, by default `git archive main fleet` into
  the work dir) installed the way deploy/install_worker.sh installs it, then
  `/usr/bin/python3 -m fleet.worker run` as uid 10001 with Nice=5 and the unit's env;
- container: the polymarket image with the supervisor's docker flags (section 6 of
  docs/workloads-design.md); its bootstrap downloads the same code from the host's /dl.
Rows of orders, order_events, fills, ledger, bets, model_scores (plus bankrolls,
models and jobs) are normalized (parity/dump.py) and compared. Exit 0 with "PARITY OK"
when equal, else the diff and exit 1; exit 2 when the run itself failed.

Why the native mode installs a tarball rather than pointing PYTHONPATH at the checkout:
a bare checkout has no fleet/VERSION, so the agent reports code_version "dev", sees the
host's 94aa556ca56a and self-updates (exit 75) on its first heartbeat. Installing main's
fleet/ as the installer does gives the same bytes under app/<version>; the harness
checks that main's tarball has the host's code_version, so both modes run identical code.

Normalized away, and why (never trading values): uuids and worker ids (random per run),
timestamps and dates (wall clock), bigserial ids and snapshot ids (they count polls,
which depend on timing; a snapshot is identified by its content instead, identical across
polls under the frozen clock), client_request_id (a hash over uuids and the snapshot id),
and the jobs' lease bookkeeping and progress (heartbeat timing).

Needs: root (chown to uid 10001, run as uid 10001), Docker, the venv with the host deps.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
if str(REPO) not in sys.path:
    sys.path.insert(1, str(REPO))

from parity import dump, scenario  # noqa: E402
from parity.hostapp import FrozenClock, HostApp, create_database, drop_database  # noqa: E402
from parity.workers import ContainerWorker, NativeWorker, Worker  # noqa: E402

DEFAULT_ADMIN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
DEFAULT_IMAGE = "polymarket-builder/polymarket:dev"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--main-checkout", type=Path, default=None, help="a checkout of main (default: git archive main fleet)")
    p.add_argument("--image", default=DEFAULT_IMAGE, help="the polymarket image (built from workloads/polymarket)")
    p.add_argument("--modes", default="native,container", help="two of native,container (native,native checks determinism)")
    p.add_argument("--ticks", type=int, default=15, help="minimum worker trade ticks per mode")
    p.add_argument("--quiet", type=int, default=4, help="trailing ticks with no proposal and no open order")
    p.add_argument("--tick-timeout", type=float, default=240.0)
    p.add_argument("--work-dir", type=Path, default=None, help="default: a new dir under /var/tmp (uid 10001 must traverse it)")
    p.add_argument("--keep", action="store_true", help="keep the databases and the work dir")
    p.add_argument("--json-out", type=Path, default=None, help="write both normalized dumps here")
    return p.parse_args(argv)


def main_fleet(args: argparse.Namespace, work: Path) -> Path:
    if args.main_checkout is not None:
        return args.main_checkout.resolve() / "fleet"
    target = work / "main-checkout"
    target.mkdir()
    archive = subprocess.run(["git", "-C", str(REPO), "archive", "main", "fleet"], capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(target)], input=archive.stdout, check=True)
    return target / "fleet"


def make_worker(mode: str, index: int, work: Path, fleet_dir: Path, image: str) -> Worker:
    root = work / f"{index}-{mode}"
    root.mkdir()
    if mode == "native":
        return NativeWorker(root, fleet_dir)
    if mode == "container":
        return ContainerWorker(root, image, f"polymarket-builder-parity-{os.getpid()}-{index}")
    raise SystemExit(f"unknown mode {mode!r}")


def run_mode(mode: str, index: int, args: argparse.Namespace, work: Path, fleet_dir: Path, plan: scenario.Plan,
             admin: str) -> dict[str, Any]:
    database_url = create_database(admin, f"{index}{mode[:3]}")
    worker = make_worker(mode, index, work, fleet_dir, args.image)
    host = HostApp(database_url=database_url, clock=plan.clock, deploy_dir=REPO / "deploy")
    facts: dict[str, Any] = {"mode": mode}
    try:
        host.start()
        facts.update(scenario.run(host, worker, work / f"{index}-{mode}", plan))
        facts["exit_code"] = worker.stop()
        facts.update(worker.describe())
        facts["rows"] = dump.dump(database_url)
        return facts
    finally:
        worker.stop()
        host.close()
        if not args.keep:
            drop_database(admin, database_url)


def report(results: list[dict[str, Any]]) -> list[str]:
    a, b = results
    lines = [f"{'table':<14} {a['mode'] + ' A':>14} {b['mode'] + ' B':>14}"]
    for table in dump.TABLES:
        lines.append(f"{table:<14} {len(a['rows'][table]):>14} {len(b['rows'][table]):>14}")
    for r in results:
        lines.append(f"{r['mode']}: python {r.get('python')}, code {r.get('code_version')}, "
                     f"ticks observed {r.get('ticks_observed')}, proposals {r.get('proposals')}, "
                     f"agent exit {r.get('exit_code')}, log {r.get('log')}")
    return lines


def run_all(args: argparse.Namespace, modes: list[str], work: Path) -> int:
    admin = os.environ.get("FLEET_TEST_DATABASE_URL", DEFAULT_ADMIN)
    start = datetime.now(timezone.utc)
    plan = scenario.Plan(gameday=(start + timedelta(days=2)).date(), clock=scenario.pick_minute(FrozenClock(start)),
                         ticks=args.ticks, quiet_ticks=args.quiet, tick_timeout=args.tick_timeout)
    print(f"work dir {work}; gameday {plan.gameday}; frozen sim minute {plan.clock.at.isoformat()}", flush=True)
    results = []
    try:
        fleet_dir = main_fleet(args, work)
        for index, mode in enumerate(modes, 1):
            print(f"running mode {index}: {mode}", flush=True)
            results.append(run_mode(mode, index, args, work, fleet_dir, plan, admin))
    except Exception:  # noqa: BLE001 - a failed run is exit 2, with the traceback
        traceback.print_exc()
        print("PARITY NOT RUN: the scenario failed (see above)")
        return 2
    if args.json_out:
        args.json_out.write_text(json.dumps([r["rows"] for r in results], indent=1, sort_keys=True), encoding="utf-8")
    print("\n".join(report(results)))
    if not results[0]["rows"]["orders"]:
        print("PARITY NOT SHOWN: the scenario placed no orders (pick another frozen minute)")
        return 2
    diff = dump.compare(results[0]["rows"], results[1]["rows"], (f"A:{modes[0]}", f"B:{modes[1]}"))
    if diff:
        print("\n".join(diff))
        print("PARITY FAILED")
        return 1
    print("PARITY OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    if len(modes) != 2 or not set(modes) <= {"native", "container"}:
        raise SystemExit("--modes takes two of native,container")
    if os.geteuid() != 0:
        raise SystemExit("run as root: the workers run as uid 10001 and their state is chowned to it")
    work = args.work_dir or Path(tempfile.mkdtemp(prefix="polymarket-parity-", dir="/var/tmp"))
    work.mkdir(parents=True, exist_ok=True)
    work.chmod(0o755)
    code = run_all(args, modes, work)
    if code == 0 and not args.keep and args.work_dir is None:
        subprocess.run(["rm", "-rf", str(work)], check=False)
    elif code != 0:
        print(f"kept {work} (worker logs under */worker.log)")
    return code


if __name__ == "__main__":
    sys.exit(main())

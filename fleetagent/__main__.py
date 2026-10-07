"""Command line: python3 -m fleetagent {enroll|run|status|specs}."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
from typing import Any

import fleetagent
from fleetagent import config, http, launch, procinfo, specs
from fleetagent.docker import Docker

ENROLL_TOKEN_ENV = "FLEET_ENROLL_TOKEN"


def _setup_logging(verbose: bool) -> None:
    """journald (and any non-tty stderr) adds its own timestamp; add ours only on a tty."""
    fmt = "%(levelname)s %(name)s: %(message)s"
    if sys.stderr.isatty() and not os.environ.get("JOURNAL_STREAM"):
        fmt = "%(asctime)s " + fmt
    logging.basicConfig(stream=sys.stderr, level=logging.DEBUG if verbose else logging.INFO, format=fmt)


def _collector() -> specs.SpecsCollector:
    return specs.SpecsCollector(Docker(binary=config.docker_binary()))


def cmd_run(args: argparse.Namespace) -> int:
    """Run the supervisor until SIGTERM/SIGINT (or one pass with --once); propagate its exit code.

    The import and start are guarded: when a self-updated version fails to start
    MAX_FAILED_STARTS times, launch.failed_start rolls app/current back to the previous
    version and exits 75 so systemd restarts the old code. Containers are never stopped.
    """
    log = logging.getLogger("fleetagent")
    state_dir = config.state_dir()
    app = config.app_dir(state_dir)
    starts = launch.note_start(app)
    if starts is not None:
        log.info("start %d of pending version %s", starts, launch.current_target(app))
    try:
        from fleetagent.supervisor import Options, Supervisor

        options = Options(background_pull=not args.once, self_update=not args.once)
        sup = Supervisor(state_dir=state_dir, options=options)
    except Exception:
        log.exception("supervisor failed to start")
        return launch.failed_start(app, starts)

    def _request_stop(signum: int, frame: object) -> None:
        log.info("signal %d received, stopping (containers keep running)", signum)
        sup.stop.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    try:
        return sup.run_once() if args.once else sup.run_forever()
    except Exception:
        log.exception("supervisor crashed")
        if starts is not None and launch.read_pending(app) is not None:
            return launch.failed_start(app, starts)
        return 1


def enroll(host_url: str, token: str, name: str | None, state_dir: str) -> dict[str, Any]:
    """Register with an enroll token and write agent.conf (0600)."""
    host_url = host_url.rstrip("/")
    machine_name = name or procinfo.hostname()
    payload = {
        "enroll_token": token,
        "name": machine_name,
        "hostname": procinfo.hostname(),
        "boot_id": procinfo.boot_id(),
        "agent_version": fleetagent.__version__,
        "specs": _collector().collect(),
    }
    resp = http.post_json(host_url + "/api/v1/machines/register", payload, timeout=15.0)
    if not isinstance(resp, dict) or not resp.get("machine_id") or not resp.get("machine_token"):
        raise SystemExit("unexpected register response")
    conf = {
        "host_url": host_url, "machine_id": str(resp["machine_id"]),
        "machine_token": str(resp["machine_token"]), "name": machine_name,
    }
    config.save_conf(state_dir, conf)
    return conf


def _read_token(args: argparse.Namespace) -> str:
    if args.token_file:
        try:
            with open(args.token_file, "r", encoding="utf-8") as fh:
                return "".join(fh.read().split())
        except OSError as exc:
            print(f"enroll failed: cannot read token file: {exc}", file=sys.stderr)
            return ""
    return args.token or os.environ.get(ENROLL_TOKEN_ENV, "")


def cmd_enroll(args: argparse.Namespace) -> int:
    state_dir = config.state_dir()
    token = _read_token(args)
    if not token:
        print(f"enroll failed: give --token, --token-file or set {ENROLL_TOKEN_ENV}", file=sys.stderr)
        return 2
    try:
        conf = enroll(args.host, token, args.name, state_dir)
    except http.HttpError as exc:
        print(f"enroll failed: {exc}", file=sys.stderr)
        return 1
    except http.HttpConnectionError as exc:
        print(f"enroll failed: host unreachable: {exc}", file=sys.stderr)
        return 1
    print(f"enrolled as {conf['machine_id']}; wrote {config.conf_path(state_dir)}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Print the conf (token redacted) and the last heartbeat result."""
    state_dir = config.state_dir()
    try:
        conf = config.load_conf(state_dir)
    except config.ConfMissing as exc:
        print(f"not enrolled: {exc}")
        return 1
    out = {"conf": config.redacted(conf), "agent_version": fleetagent.__version__, "status": config.load_status(state_dir)}
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


def cmd_specs(args: argparse.Namespace) -> int:
    """Print the specs the supervisor would report (disk detection included)."""
    out = _collector().collect()
    out["native_polymarket"] = specs.native_polymarket(subprocess.run)
    out["hostname"] = procinfo.hostname()
    out["boot_id"] = procinfo.boot_id()
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m fleetagent", description="fleet machine supervisor")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the supervisor (systemd)")
    run.add_argument("--once", action="store_true", help="one register, heartbeat and reconcile pass, then exit")
    run.set_defaults(func=cmd_run)
    enr = sub.add_parser("enroll", help="register with an enroll token and write agent.conf")
    enr.add_argument("--host", required=True, help="host URL, e.g. https://host.tailnet.ts.net")
    enr.add_argument(
        "--token", default=None,
        help=f"enroll token from the dashboard (or set env {ENROLL_TOKEN_ENV}, which keeps it off the command line)",
    )
    enr.add_argument("--token-file", default=None, help="read the enroll token from this file")
    enr.add_argument("--name", default=None, help="machine name (default: hostname)")
    enr.set_defaults(func=cmd_enroll)
    status = sub.add_parser("status", help="print the conf and the last heartbeat result")
    status.set_defaults(func=cmd_status)
    spec = sub.add_parser("specs", help="print the detected specs (RAM, CPUs, Docker disk and its type)")
    spec.set_defaults(func=cmd_specs)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())

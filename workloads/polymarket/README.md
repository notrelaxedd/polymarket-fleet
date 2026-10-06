# polymarket workload

The Polymarket worker agent (`python3 -m fleet.worker run`) as a fleet workload. This
folder is only a wrapper: the agent's code stays in `fleet/` (shipped by the host's `/dl`
exactly as for a native worker) and the trading logic stays in `host/`. Nothing about
trading changes: the container speaks the existing worker protocol with its own worker
token, its orders go through the same automatic host approval, kill switch and live
switch, and it declares no outbound actions (docs/workloads-design.md sections 0 and 7).

Files:
- `workload.toml`: the manifest (section 2 of the design): `protocol = "fleet-worker"`,
  `mode = "service"`, host network and UTS, `/state` volume, 85% memory cap, nice 5,
  15 s stop timeout, no restart after exit 78, one container secret `FLEET_ENROLL_TOKEN`,
  `can_trade = true` (the pin rule applies).
- `Dockerfile`: `ARG BASE_IMAGE=debian:trixie-slim`; installs `python3` and
  `ca-certificates` with apt only when the base has no `python3` (production: the
  Debian 13 package, the same Python build a native worker runs); user `fleet` uid 10001;
  `ENTRYPOINT ["python3", "/opt/fleet/bootstrap.py"]`. No Polymarket code in the image.
- `bootstrap.py` (stdlib): sets nice, installs `/state/app/<version>` from `/dl` when
  `/state/app/current` is missing (sha256 and tar-member checks identical to the
  installer and `fleet/worker/update.py`), enrolls when `/state/worker.conf` is missing
  (token from `/run/fleet/secrets/FLEET_ENROLL_TOKEN`, passed in the environment; no
  token file means exit 78), then `execve`s `python3 -m fleet.worker run` with
  `PYTHONPATH=/state/app/current` and `FLEET_STATE_DIR=/state`. The agent is the
  container's main process, so its exit codes (75 restart after self-update, 78 stop)
  reach the supervisor unchanged. After the first start the agent's own self-update and
  rollback own `/state/app`, as they own `/var/lib/fleet/app` natively.

Build (on the host only; machines never build): `tools/workloads/publish.sh polymarket`.
Where apt cannot run (an offline sandbox):
`docker build --build-arg BASE_IMAGE=python:3.13-slim -t <name> workloads/polymarket`.

## Runtime equivalence with `deploy/fleet-worker.service`

| systemd unit (native) | container (supervisor flags, manifest) | Same? |
|---|---|---|
| `ExecStart=/usr/bin/python3 -m fleet.worker run` | bootstrap `execve(sys.executable, -m fleet.worker run)` | yes; `/usr/bin/python3` on the Debian base, `/usr/local/bin/python3` on python:3.13-slim |
| `Environment=PYTHONPATH=/var/lib/fleet/app/current` | `PYTHONPATH=/state/app/current` | yes (path differs, layout identical) |
| `Environment=FLEET_STATE_DIR=/var/lib/fleet` | `FLEET_STATE_DIR=/state` (bind mount of `/var/lib/fleet-workloads/polymarket/state`) | yes |
| `User=fleet` (system uid) | `--user 10001:10001` | yes after the migration's `chown` |
| `Nice=5` | `nice = 5` (bootstrap `os.nice`, inherited through `execve`) | yes (checked: `/proc/1/stat` nice 5) |
| `MemoryMax=85%` | `memory_max_pct = 85`: `--memory <85% of RAM>m --memory-swap -1` | yes (swap unlimited in both) |
| `TimeoutStopSec=15` | `stop_timeout_s = 15`: `docker stop -t 15` | yes |
| `Restart=always`, `RestartSec=3` | supervisor restarts an exited container after 3 s | yes (supervisor rule) |
| `RestartPreventExitStatus=78` | `no_restart_exit_codes = [78]` | yes |
| `NoNewPrivileges=yes` | `--security-opt no-new-privileges` | yes |
| `ProtectSystem=strict` + `ReadWritePaths=/var/lib/fleet` | `--read-only` + writable `/state` (and `/scratch`) | yes |
| `PrivateTmp=yes` | `--tmpfs /tmp:rw,nosuid,size=256m` | private in both; the container's is capped at 256 MB (the agent writes nothing to /tmp) |
| host network, host name | `--network host`, `--uts host` | same IP and hostname |
| `/proc/sys/kernel/random/boot_id` | same kernel, same boot id | yes; the host links machine and worker by it |
| init (systemd) reaps orphans | the agent is PID 1 and does not reap orphans | no: see "Differences" |
| journald | docker `local` log driver, shipped by the supervisor | logging only |

## Differences found (none changes an order, a fill or a ledger row)

- **PID 1.** In the container the agent is PID 1 in its own PID namespace. Orphaned
  grandchildren (pool workers of a killed model search) are reparented to the agent,
  which never reaps unknown children, so they stay zombies until the container restarts
  (checked: a Python PID 1 keeps `Z` entries, `docker run --init` does not). Natively
  systemd reaps them. Fix: the supervisor should start this container with `--init`.
  Also, SIGTERM before the `execve` (during the first download) is ignored by PID 1 and
  ends with SIGKILL after 15 s.
- **Python binary.** Production: the Debian 13 `python3` package in both. The sandbox
  parity run used python:3.13-slim (3.13.16, source build, `/usr/local/bin/python3`)
  against Ubuntu's 3.13.16 natively; the trained model's floats were bit-identical.
- **Environment.** The container also carries the image's variables (`LANG=C.UTF-8`,
  `PYTHON_VERSION`, `GPG_KEY` on python:3.13-slim; nothing on debian:trixie-slim) and
  the supervisor's `FLEET_HOST_URL`, `FLEET_WORKLOAD`, `FLEET_MACHINE_ID`, `FLEET_EPOCH`.
  The bootstrap removes `FLEET_RUN_TOKEN` and `FLEET_ENROLL_TOKEN` before the `execve`.
  The agent reads none of them. The container's time zone is UTC; the worker code does
  not use local time.
- **/proc.** `/proc/meminfo` and `/proc/stat` show the whole machine in both (no lxcfs),
  so RAM, CPU % and the memory watchdog read the same values; the cgroup cap is not
  visible to the agent in either mode.
- **The enroll token file** stays in `/run/fleet/secrets` while the container runs
  (natively it never touches disk after the install). The supervisor deletes it when the
  container stops; the agent never reads it.

## Native to container migration (by hand, non-trading machines first)

1. On `/fleet`: set the worker idle and wait until it reports idle, then disable it.
2. On the machine: `sudo systemctl disable --now fleet-worker`.
3. Move the state, keeping the identity and the installed code:
   ```
   sudo mkdir -p /var/lib/fleet-workloads/polymarket/state
   sudo mv /var/lib/fleet/* /var/lib/fleet-workloads/polymarket/state/
   sudo chown -R 10001:10001 /var/lib/fleet-workloads/polymarket/state
   ```
   `app/current` is a relative symlink, so it stays valid after the move.
4. On `/machines`: assign `polymarket` (refused while `fleet-worker` is active).
5. On `/fleet`: enable the worker again. Same worker id, same token, same code version:
   the bootstrap finds `app/current` and `worker.conf` and starts the agent at once.

A machine without a native worker needs the `FLEET_ENROLL_TOKEN` secret set on the
workload page before the assignment; the first start downloads the code and enrolls.

## Rollback (container to native)

1. On `/fleet`: set the worker idle; on `/machines`: assign `none` (the host drains the
   worker first), and wait until the container is stopped.
2. Move the state back and give it to the native user:
   ```
   sudo mv /var/lib/fleet-workloads/polymarket/state/* /var/lib/fleet/
   sudo chown -R fleet:fleet /var/lib/fleet
   sudo systemctl enable --now fleet-worker
   ```
3. On `/fleet`: enable the worker. If `/var/lib/fleet` is gone, rerun the installer
   (`curl -fsSL HOST_URL/install.sh | sudo bash -s -- HOST_URL`); it keeps an existing
   `worker.conf`.

## Parity check

`sudo PYTHONPATH=$PWD .venv/bin/python tools/workloads/paper_parity.py` runs the same
frozen-clock paper scenario (a train job, three paper assignments, settlement) with the
agent native and in this container and compares the normalized orders, order events,
fills, ledger, bets and model scores (plus bankrolls, models and jobs). It needs root,
Docker, Postgres (`FLEET_TEST_DATABASE_URL`) and an image (`--image`).

## Not verified

- The production base (debian:trixie-slim with the apt step): the build sandbox has no
  internet, so only the python:3.13-slim path was built and run.
- A real systemd unit as the native side: the parity run starts the native agent as a
  plain process with the unit's environment, uid 10001 and nice 5, without
  `ProtectSystem`, `PrivateTmp` or `MemoryMax`.
- The real supervisor (`fleetagent`): the parity run starts the container with the
  flags of design section 6 by hand. Restart after exit 75 was checked by hand
  (`docker start` after the agent's self-update ran the new code); exit 78 and the 3 s
  restart are the supervisor's.
- Live trading, the kill switch and the release handshake inside the container (the
  parity scenario is paper only; the e2e suites cover them natively).
- A pooled model search (`multiprocessing` fork workers) inside the container, the
  tailnet (MagicDNS, `tailscale serve` TLS) from the container, and arm64 machines.

# Workloads

A workload is a folder with a manifest, a Dockerfile and its own code. The fleet host stores the manifest, you publish the image to the host's registry, and you assign the workload to a machine; the machine's agent pulls the image by digest and runs it as one container. One machine runs one workload at a time. The design and every fixed name below live in `docs/workloads-design.md`.

```
workloads/
  _template/    copy this to start a new workload (skipped by the host)
  hello/        the smallest working example: kind "hello"
  <name>/
    workload.toml        the manifest
    Dockerfile           builds the image (on the host, never on a machine)
    app/main.py          the code
    app/fleet_client.py  the SDK, identical in every workload
```

Everything that runs on a machine is standard library only: no `apt-get` and no `pip` in a build.

## Manifest reference (`workload.toml`)

Parsed by `host/workloads/manifest.py`, which lists every problem at once. `workloads/_template/workload.toml` has a comment on every key.

| Key | Meaning |
|---|---|
| `schema` | Always 1. |
| `name` | `^[a-z][a-z0-9-]{1,31}$`, equal to the folder name. |
| `description` | At most 300 characters. |
| `image` | Registry repository such as `fleet/hello`. No tag, no digest: the digest is recorded when you publish. |
| `protocol` | `workload-v1` for every new workload (`fleet-worker` is Polymarket's own). |
| `[resources] min_ram_mb` | Placement refuses a machine with less total RAM. |
| `[resources] min_disk_mb` | Placement refuses a machine whose free Docker disk is below this plus the image size. |
| `[resources] write_heavy` | `true` is refused on flash storage (SD, USB) and on disks of unknown type. |
| `[resources] memory_max_mb` or `memory_max_pct` | Memory cap (not both); neither means no cap. |
| `[resources] cpus` | Optional CPU limit. |
| `[runtime] mode` | `jobs` (claims from the host queue) or `service` (long running). |
| `[runtime] job_kinds` | Kinds the workload handles; required for `jobs`. |
| `[runtime] uid` | Container user; owns `/state` and the secret files. Default 10001. |
| `[runtime] state_volume` | `true` mounts a persistent `/state`. |
| `[runtime] scratch_mb` | `/scratch` size hint. Default 512. |
| `[runtime] stop_timeout_s` | Grace between SIGTERM and SIGKILL. Default 15. |
| `[runtime] no_restart_exit_codes` | Exit codes after which the container is not restarted. Default `[78]`. |
| `[runtime] nice` | 0 to 19. |
| `[secrets] container` | Names delivered as files in `/run/fleet/secrets`. |
| `[secrets] host_only` | Names the host keeps for its own senders (for example `SMTP_URL`); never delivered to a machine. Needs `outbound.actions`. |
| `[outbound] actions` | Subset of `["email", "log"]`. Non-empty means every send waits for your approval. |
| `[trading] can_trade` | Polymarket only. |

`network = "host"`, `uts_host` and `can_trade` are accepted only with `protocol = "fleet-worker"`.

## The container contract

The agent starts the container with `docker run` and these settings. A workload may rely on them and must not need more.

- **Environment**: `FLEET_HOST_URL` (the host's address), `FLEET_RUN_TOKEN` (a token scoped to this machine, this workload and this assignment), `FLEET_WORKLOAD`, `FLEET_MACHINE_ID`, `FLEET_EPOCH` (the assignment counter). Treat the token as a secret: never print it. The SDK reads all of these in `Client.from_env()`.
- **Secrets**: one file per name in `/run/fleet/secrets/<NAME>` (mode 0400, owned by `uid`, mounted read-only, on tmpfs on the machine). `client.secret(NAME)` returns the text or `None`. They are written when the container starts and deleted when it stops; they exist only on the machine the workload is assigned to. The agent replaces every known secret value with `[redacted]` in the logs it ships, but do not rely on that: do not print secrets.
- **Files**: the root filesystem is read-only. Writable places: `/scratch` (wiped at every start and stop; the SDK gives each job `/scratch/<job id>` and deletes it when the job ends), `/tmp` (a 256 MB tmpfs) and, with `state_volume = true`, `/state` (kept across restarts and image updates).
- **Process**: runs as `uid:uid` with `no-new-privileges`, under the memory cap and `cpus` of the manifest, on the default bridge network. The container has the network access of the machine; it can reach the host's address and the internet unless the machine is restricted.
- **Logs**: standard output and error go to Docker's `local` log driver (10 MB, 3 files) and are shipped to the host (at most 200 lines per heartbeat; the last lines per machine show on the dashboard). Print progress, not data.
- **SIGTERM**: Docker sends it on unassign, reassign, update and reboot, then SIGKILL after `stop_timeout_s`. The SDK's `run_forever` handles it: the current job is released with reason `shutdown` (it goes back to the queue with its checkpoint) and the process exits 0. Own loops must do the same within `stop_timeout_s`.
- **Exit codes**: 0 is a normal stop. Any exit is restarted after 3 seconds unless the code is in `no_restart_exit_codes`; then the machine shows `failed` and waits for you. Exit 78 means "configuration is wrong, do not retry" (for example a required secret is missing). When the host answers 401 the run token is no longer valid, which means the machine was reassigned: the SDK exits 0 and releases nothing; the agent, which learns of the new assignment on its next heartbeat, stops or replaces the container.
- **Outbound actions**: a workload never sends email or anything else on its own authority. It queues an action with `client.outbound(kind, payload, dedupe_key, job)`; it is `pending` until you approve it on the Approvals page (or `host.cli outbound --approve ID`), and only then does the host send it, with credentials the container never holds. `outbound_status(id)` shows `pending`, `approved`, `sent`, `rejected`, `failed` or `expired`. The `dedupe_key` makes a retry return the same action instead of a second one. The workload must declare the kind in `outbound.actions`. Honest limit: this is enforced by who holds the credentials, not by a firewall; a container that has its own credentials or network access can still reach the internet.
- **Cleanup duties**: delete everything you create outside `job.scratch` and `/state`; never grow `/state` without bound; do not keep data you need only for the job. The agent removes old images, stopped containers and the build cache and wipes scratch and secrets on stop, so a machine returns to a clean state after you unassign. A job's scratch folder is deleted by the SDK after `complete`, `fail` or `release`.

### Jobs

For `mode = "jobs"` the host queue `workload_jobs` holds the work. The SDK calls `POST /api/v1/wl/claim`, `.../jobs/{id}/heartbeat`, `.../release`, `.../complete`, `.../fail` and `/api/v1/wl/outbound` with the run token (section 5.2 of the design). A claimed job has a lease; a progress call renews it, and so does a background thread every `lease_seconds / 3`. A job that stops renewing is requeued; after 3 expiries it fails. If the answer to a job call is 409 the job is no longer yours: stop work on it (the SDK raises `LeaseLost`, drops the job and its scratch, and goes on).

```python
from fleet_client import Client, Job, run_forever

def handle(job: Job) -> dict:
    job.progress(0.5, {"step": 1})        # also renews the lease; the checkpoint survives a requeue
    (job.scratch / "x.txt").write_text("...")
    return {"ok": True}                    # the result; raise to fail the job

if __name__ == "__main__":
    raise SystemExit(run_forever({"mykind": handle}))
```

`job.cancelled` (a `threading.Event`) is set when you cancel the job on the host: call `job.release("cancelled")` and return `None`. Paths can be overridden for tests with `FLEET_SECRETS_DIR` and `FLEET_SCRATCH_DIR`.

## Publish and assign

On the host (see the README section "Workloads" for the one-time setup of the registry and `secrets.env`):

1. Sync the manifests into the database: `docker compose exec host python -m host.cli workloads-sync` (or the Sync button on `/workloads`). The `host` image contains `workloads/`, so rebuild it first when a manifest changed: `docker compose up -d --build host`.
2. Build, push and record the image: `tools/workloads/publish.sh <name>`. The workload is not assignable until this has run once.
3. Set its secrets: `docker compose exec -T host python -m host.cli secret-set <name> <SECRET>` (the value is read from standard input) or the form on `/workloads/<name>`. Values are encrypted in Postgres and never shown again.
4. Assign it to a machine on `/machines` (the dropdown lists every workload; those that do not fit are greyed with the reason) or with `host.cli assign <machine> <name>`. Assigning another workload (or `none`) stops the old container, wipes its scratch and secrets and lets the machine's cleanup remove images that are no longer needed.
5. Send work: `host.cli wl-send-job <name> <kind> --params '{...}'` or the form on `/workloads/<name>`.

Refusals you can meet: 422 with codes `ram_too_small`, `disk_too_small`, `write_heavy_on_flash`, `image_not_published`, `docker_missing`, `workload_disabled`; 409 when the machine is pinned (it runs live trading) or still runs the native `fleet-worker`.

## The hello workload

`workloads/hello/` is the example. Kind `hello`, parameters `{"name": str, "steps": 1..60 (default 3), "notify": bool}`. It reads the secret `HELLO_GREETING` (default `Hello`), writes and reads back a file in the job's scratch folder at every step, reports progress per step and returns `{"greeting": "<greeting>, <name>!", "machine", "epoch", "steps"}`. With `notify` it also queues a `log` outbound action (dedupe key `hello:<job id>`) and puts its id in the result as `outbound_id`; the action stays `pending` until you approve it. The host replaces any stored secret value in job results, errors and checkpoints with `[redacted]`, so a greeting set from `HELLO_GREETING` shows as `[redacted], <name>!` on the dashboard. `tools/workloads/local_demo.sh` runs it end to end on one machine.

# Making a new workload

This folder is the template. Folders whose name starts with `_` are skipped by the host, so nothing here is ever synced or run. The full reference is `workloads/README.md`; this page is the short path.

1. Copy the folder and name it: `cp -r workloads/_template workloads/my-job`. The folder name, the `name` in `workload.toml` and the image repository (`fleet/my-job`) should all agree.
2. Edit `workload.toml`. Every key is commented. The ones to decide first: `job_kinds`, `min_ram_mb`, `min_disk_mb`, `write_heavy` (set it if the job writes a lot; it is then refused on SD cards and USB sticks), `memory_max_mb`, the secrets it needs and the outbound actions it may queue.
3. Write the work in `app/main.py`. The skeleton has one handler for the kind `work`: read parameters from `job.params`, call `job.progress(fraction, checkpoint)` now and then, keep temporary files in `job.scratch`, and return a small dict (the job's result) or raise to fail the job. Read secrets with `client.secret("NAME")`. Queue emails or log lines with `client.outbound(kind, payload, dedupe_key, job)`; they wait for your approval.
4. Leave `app/fleet_client.py` alone. It is the SDK, standard library only, and a test requires every workload to carry the same bytes. To change it, change the template's copy, then copy it into every workload.
5. Keep the Dockerfile standard-library only: no `apt-get` and no `pip` in the build. If you need a package, vendor it into `app/` or ask first; machines have no build step of their own.
6. Check it locally without the host: `python -m pytest tests/test_wl_manifests.py` parses every manifest and runs the SDK against a fake host. To run your own app against the fake host, copy the pattern in that test (`FakeHost`, then the environment variables `FLEET_HOST_URL`, `FLEET_RUN_TOKEN`, `FLEET_SECRETS_DIR`, `FLEET_SCRATCH_DIR`).
7. Publish and use it: `tools/workloads/publish.sh my-job`, then sync, assign and send a job as described in `workloads/README.md` ("Publish and assign").

Rules that every workload keeps: never print a secret or the run token (stdout and stderr are shipped to the host), delete what you create outside `job.scratch` and `/state`, finish or release the current job within `stop_timeout_s` of SIGTERM, and exit 78 only for "configuration is wrong, do not restart".

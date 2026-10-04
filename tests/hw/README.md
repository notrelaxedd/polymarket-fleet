# Hardware tests

These tests need the real stack: the host running in Docker Compose and at least one real worker machine enrolled. They are not part of `pytest tests/` and CI does not run them.

## roletest.sh

```bash
tests/hw/roletest.sh <worker-id> [compose-dir]
```

Sends the worker a long sleep job, flips its role to `train`, and prints the seconds between the request and the worker's acknowledgement. It exits 0 if that is 10 seconds or less, and 1 if it is slower or the worker never acknowledges. The worker is left in the `train` role. `compose-dir` defaults to the repository root.

When to run it: after installing a new worker, after changing the agent, the drain logic or the heartbeat interval, and as the step 2 owner test. A passing number on the real network (Tailscale latency included) is the evidence that the role handshake meets the 10 second target.

## screenshots.py

```bash
.venv/bin/python -m pip install playwright   # the python package only; no "playwright install"
PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers .venv/bin/python tests/hw/screenshots.py /tmp/screenshots-step5
```

A dev tool, not a pytest test (`tests/hw/conftest.py` keeps it out of collection). It seeds a throwaway database with three workers (running, switching, offline) and a few jobs, the step 3 models (a real short search on the nflverse fixture) and the step 4 trading rows (`seed_step4.py`: games with sim markets, a trade worker, assignments, orders, a fill, a settled bet), serves the dashboard with `FLEET_DEV=1`, and writes PNGs of the fleet, jobs, job detail, settings, models, model detail and trading pages at 390x844 and 1280x800 in light and dark, then (step 5, `seed_step5.py`) the settings, trading and fleet pages with live on (the Live trading group on, a live assignment with its exchange order, a resting smoke order, the LIVE pill) and the fleet, settings and trading pages after an auto-kill (the red bar naming the reason), plus the fleet and trading pages after a hand `POST /kill` and the trading page after the reset. At phone width it fails (exit 1) on horizontal scroll, on a button, select or link inside a worker card or any form control under 40 px tall, and when the fragment refresh does not reset the "updated N s ago" footer. It needs the Chromium build that the installed Playwright release expects under `PLAYWRIGHT_BROWSERS_PATH` (release 1.56 pairs with build 1194).

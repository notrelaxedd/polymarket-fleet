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
.venv/bin/python -m pip install "playwright==1.56.0"   # the python package only (1.56 pairs with Chromium build 1194); no "playwright install"
PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers .venv/bin/python tests/hw/screenshots.py /tmp/screenshots
```

A dev tool, not a pytest test (`tests/hw/conftest.py` keeps it out of collection). It seeds a throwaway database with three workers (running, switching, offline) and a few jobs, then:

- the step 3 models (`seed_step3.py`: a real short search on the nflverse fixture, a trained child, a backtest);
- the step 6 rows (`seed_step6.py`: validation-era metrics and stress tables on the search lineages, two ranked, one flagged overfit, one left "not validated", a finished validate job);
- the step 4 trading rows (`seed_step4.py`: games with sim markets, a trade worker, assignments, orders in every state, a fill, a settled bet) and the cached paper CLV interval;
- the step 6B rows (`seed_step6b.py`: an epa_blend lineage ranked on snapshot replay CLV with its Snapshot replay section, snapshot columns on the paper-ranked lineage and a 22-bet one on the unvalidated lineage, a finished snapshot backtest job, a position partly sold through the real sell approval and fill path with an open sell on the rest, and a second assignment holding a losing position). `check_step6b` asserts the pages show all of it before any capture;
- after that check, the step 6C rows (`seed_step6c.py`): two ingame_wp lineages (one `paper_ok`, beating vegas_wp on 41,812 held-out plays, one `candidate` that does not), DEN @ LAC in its third quarter (ESPN event 401800077) whose game-state rows are what `parse_summary` extracts from `tests/fixtures/espn_summary_in.json` with the teams renamed, kept 3 s old while the captures run, an in-game paper assignment on it holding a partly filled in-game buy approved through the real in-game checks, feed_lag rows (12 measured ESPN summary events, median 6 s behind the market; 3 ESPN scoreboard events, "not enough data"), and yesterday's SF @ LA settled with a pre-game bet and two in-game bets, so the ingame_wp lineage has a paper record. `check_step6c` asserts the pages show it. The probe never reaches ESPN: `stub_espn` replaces `host.exchange.gamestate.default_fetch` in the screenshot process (the server runs in it) with the renamed fixture.

It serves the dashboard with `FLEET_DEV=1` and writes PNGs of the fleet, jobs (the backtest form with its price source and the replay hint), job detail, settings (Trading, Snapshot replay, nflverse signals and In-game groups), models (validation, paper and snapshot columns, the snapshot rank chip, the In-game models table), model detail (the Robustness section), the flagged model, the snapshot-ranked epa_blend model (`model-snapshot`), the search, backtest, validate and snapshot backtest (`job-replay`) results, the validate form, the ingame_wp model (`model-ingame`: held-out validation against vegas_wp, by period, by score, calibration), trading (positions, sell chips, the live score and clock with the state age, the in-game model probability beside the mid, `in-game` chips on orders and fills, the In-game feed block), the New assignment form with an ingame_wp model preselected (`trading-assign-ingame`) and the game-state probe page (`probe-gamestate`: the "Probe game state" form submitted in the browser) at 390x844 and 1280x800 in light and dark, then (step 5, `seed_step5.py`) the settings, trading and fleet pages with live on, after an auto-kill (the red bar naming the reason), after a hand `POST /kill`, and the trading page after the reset. At phone width it fails (exit 1) on horizontal scroll, on a button, select or link inside a worker card or any form control under 40 px tall, and when the fragment refresh does not reset the "updated N s ago" footer; on the Models and ingame_wp model pages it also fails when a non-stacked table scrolls sideways or an in-game reason line is clipped at 390, and at 1280 when a Models table-wrap scrolls sideways, a summary is under 200 px or an action button sits outside its table. It needs the Chromium build that the installed Playwright release expects under `PLAYWRIGHT_BROWSERS_PATH`: release 1.56 pairs with build 1194, the one in `/opt/pw-browsers`; a newer release looks for a build that is not there and fails at launch.

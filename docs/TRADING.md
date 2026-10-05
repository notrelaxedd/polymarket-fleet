# Trading (step 4: paper, step 5: live)

Money is integer cents. Prices are decimals in (0, 1) per $1 contract, rounded to the
market tick. Sizes are whole contracts. One assignment = one game, one model, one mode,
one bankroll. Workers only propose; the host approves inside one transaction; the
exchange process executes. Paper is the default and only mode until step 5.

## Processes

- `fleet-host` (FastAPI): worker and owner APIs, order approval, kill switch, dashboard,
  reaper/dispatcher/orphan loop.
- `fleet-exchange` (`python -m host.exchange.main`, its own compose service, its own env
  file `exchange.env` that from step 5 holds the Polymarket US keys): market discovery
  and price snapshots, the executor outbox (submit, cancel), fills (paper simulator or
  live), settlement and scoring, scores fetch, snapshot retention and bars, the
  fleet-wide rate limiter, and a heartbeat row in `exchange_state` every 5 s. It is the
  only process that ever talks to an exchange.

## Market data sources (`host/exchange/adapters/`)

One interface, three implementations, chosen by `settings.market_source`:
```python
class MarketSource:
    name: str
    def list_markets(self, games, lookahead_days) -> list[MarketInfo]   # tradable YES contracts
    def fetch_book(self, market_ref) -> Book                            # bids/asks, up to 10 levels
class OrderGateway:                                                     # live only (step 5)
    def place(...), cancel(...), open_orders(), fills(since), balance()
```
`MarketInfo(platform, market_ref, event_ref, title, home_team, away_team, side, kickoff_at,
tick, min_size, raw)`; `side` says which team the YES contract pays. `Book(bids, asks,
fetched_at)` with levels `[price, size]` sorted best first.

- `sim`: deterministic synthetic markets for every game in the next `market_lookahead_days`
  with both moneylines: two markets per game (home YES, away YES); mid = devigged nflverse
  moneyline (0.5 when missing) plus a seeded random walk (`random.Random(game_id + minute)`,
  steps of 0.005, clamped 0.03..0.97); spread 0.02; 5 levels per side with sizes 50..500;
  liquidity in the thousands of dollars. Lets the whole paper flow run with no network.
- `polymarket_us`: public reads from the Polymarket US gateway (no key). The endpoint paths
  and field names are UNVERIFIED (docs unreachable from the build sandbox): keep them in
  `settings.market_source_config.polymarket_us` (base_url, markets_path, book_path, field
  names) with defaults from the research notes, parse defensively, and log the raw payload
  at DEBUG on any parse failure. The owner endpoint `POST /api/exchange/probe` returns the
  raw (truncated) markets payload so the owner can paste it back for fixing.
- `polymarket_clob`: the public offshore API as a price source only (never for orders):
  Gamma `GET {gamma_url}/events?tag_slug=nfl&closed=false&limit=200` (events with `markets`,
  each market with `question`, `outcomes` and `clobTokenIds` as JSON-encoded lists,
  `gameStartTime`/`endDate`), CLOB `GET {clob_url}/book?token_id=...` (`bids`/`asks` of
  `{price, size}`). Same defensive parsing and probe.

Mapping markets to games (`host/exchange/mapping.py`): team names and codes resolve through
one alias table (`host/exchange/teams.py`: full names, nicknames, codes, old codes); a
market maps to the game with the same two teams whose kickoff is within 36 h of the
market's start (or whose gameday matches). `mapping_confidence` 1.0 and `mapping_confirmed`
true when both teams and the date match exactly; otherwise the market is listed under
"unmatched markets" on `/trading` and the owner links it by hand. Unconfirmed markets are
never traded.

## Snapshots and closing price

The poller fetches books for mapped, unresolved markets of games within the lookahead:
every `snapshot_active_s` (2) for markets with an active assignment, every
`snapshot_idle_s` (30) otherwise, each fetch costing one market-data token. A snapshot
stores bid, ask, mid, up to 10 levels per side and `liquidity_usd_cents` = dollar depth
within 5 cents of the touch on both sides. `markets.best_bid/best_ask/liquidity` mirror the
latest snapshot. `closing_price` = mid of the last snapshot strictly before `kickoff_at`
(fallback: the last snapshot), frozen when the game kicks off and never changed; a game
settled before its kickoff (`simulate-final`) freezes it at settlement by the same rule,
so every bet has a CLV. Raw rows
are deleted after `snapshot_retention_days`; a nightly roll-up keeps 1-minute bars in
`price_bars` (open/high/low/close of mid, last bid/ask, min liquidity, count). Bets and
closing prices are frozen before any deletion.

## Assignments, bankrolls, ledger

The owner creates an assignment (game, model, mode, bankroll dollars) from `/trading` or the
Models page. Rules: the model's lineage must not be retired; `paper` needs nothing else;
`live` needs `live_enabled`, lineage `live_eligible` and exchange auth (step 5); at most
`max_paper_models_per_game` paper assignments per game and exactly one live. Creating one
funds a bankroll (`ledger fund`) and inserts a `trade` job (`params.assignment_id`,
`max_expiries NULL`). Halting an assignment cancels its open orders and leaves the job
leased; settling it completes the job.

Ledger conventions (every row signed, cents; `bankrolls` is the cached sum, a nightly
replay asserts `initial + realized = available + reserved + open_cost`):
- `fund`: `+initial available`.
- `reserve` (approval): `-cost available, +cost reserved` where `cost = size * (price + fee) * 100`.
- `release` (cancel, expiry, rejection by the exchange, unfilled remainder): the reverse for
  the unfilled part.
- `fill` (a buy fill): `-(price*size + fee)*100 reserved, +price*size*100 open, -fee*100
  realized`; `fills.basis_cents = price*size*100`.
- `sell` (a sell fill, step 6 Part B): `-basis open, +(proceeds - fee) available,
  +(proceeds - fee - basis) realized` with `proceeds = price*size*100` and `basis` the
  average-cost basis the sale removes (`fills.basis_cents`); no reservation is touched.
- `settle`: `-basis open, +payout available, +(payout - basis) realized` over the
  contracts still held (the remaining basis after any sales); payout is
  `size * 100` on the winning side, 0 on the losing side, `basis` on a push (tie).
- `adjust`: owner correction with a note (audit row).

## Trade worker tick (`fleet/worker/trade.py`)

A worker in the `trade` role claims up to `trade_max_games` trade jobs (heartbeat
`want_jobs = free slots`). Every `trade_tick_s` (5) it calls `GET /api/v1/trade/state` and,
per active assignment whose game has not kicked off (when `trade_pregame_only`):
1. `market_p_home` = devig of the two markets' mids (one market: its mid for that side);
   `features` from the game row; `my_p = model.predict(game, market_p_home, features)` using
   the artifact from the state payload (cached by model id).
2. For each market (side): `p_side`, `price = ask`, `fee = taker_rate * price * (1 - price)`,
   `cost = price + fee`, `edge = p_side - cost`. If `edge >= min_edge`, no open order on that
   market, and `available > 0`: the Kelly stake is a target position,
   `target = floor(kelly_fraction * equity * edge / (1 - cost))` cents with
   `equity = available + reserved + open_cost`; `stake = target - basis_cents of the
   position already held on that market`, capped at `min(max_bet, available)` where
   `max_bet = min(assignment.max_bet_cents, settings.max_bet_cents)` (both travel in the
   state payload); `size = min(floor(stake / (cost * 100)), floor(participation * depth at
   or below the ask))` contracts; propose when `stake > 0` and `size >= min_size`. A filled
   position therefore stops the worker re-buying the same edge every tick.
   `client_request_id = sha256(assignment|market|snapshot_id|price|size)[:32]`.
3. Cancel its own open orders whose edge at the current ask is below 0.
4. Under `kill`, or when the assignment is halted, or after kickoff: propose nothing.
Rationale text (one line: "my 0.57 vs ask 0.52, fee 0.012, edge 0.038") travels with the
request and is shown on `/trading`.

## Approval (`host/limits.py`, `approve_order`, one transaction)

`pg_advisory_xact_lock(hashtext('approve:' || mode))` serialises approvals and the kill
per mode; the bankroll row is `FOR UPDATE`. Everything that halts, releases or settles
takes the same lock first: `halt_assignment` (the owner's Halt, the live daily-loss trip,
an eligibility demotion, a cancelled trade job), `POST /api/v1/trade/release`,
`settle_game` and `cancel-all`, so an approval in flight can never commit onto an
assignment, job or game those have just closed. Checks, in order, each with a stable
`reject_reason` code:
1. `duplicate`: `client_request_id` seen -> return the stored decision.
2. `killed`: `kill_switch`.
3. `lease`: job leased by this worker with this `lease_token`, lease not expired, not
   `preempt_requested` or `cancel_requested`, worker `desired_role = trade`. The job row
   is read `FOR SHARE` after the bankroll lock, so a release committing at the same
   time (heartbeat `released[]`, the reaper, `/trade/release`) is waited for and its
   queued row is what the check sees.
4. `assignment`: active; `market`: mapped to the assignment's game, confirmed, unresolved;
   `kickoff`: game not started when `trade_pregame_only`.
5. `mode`: live needs `live_enabled`, lineage `live_eligible`, `exchange_state.auth_ok`
   and a market on the live platform (the current `market_source`, never
   `polymarket_clob`; step 5).
6. `stale_book`: cited `snapshot_id` belongs to the market and is no older than
   `book_max_age_s`; being the latest snapshot does not spare it (a stalled poller's
   newest book is still a dead book).
7. `liquidity`: cited and latest snapshot liquidity `>= liquidity_floor_cents`.
8. `participation`: `size <= participation * depth at or better than price`.
9. `price_band`: `0.01 <= price <= 0.99` and `price <= ask + 0.05`.
10. `max_bet`: `cost + cost of open orders on the same market <= min(assignment.max_bet,
    settings.max_bet_cents)`.
11. `bankroll`: `cost <= available`.
12. `daily_loss`: `losses_today + cost > max_daily_loss_cents[mode]` rejects, where
    `losses_today = max(0, -(realized_today + unrealized))`, realized from `ledger`
    `d_realized` in the owner's day and unrealized = mark-to-mid of open positions.
    When `losses_today >= max_daily_loss` on its own: live -> `live_enabled=false`, live
    assignments halted, live orders cancelled (audit `daily_loss_trip`); paper -> keep
    rejecting until the day rolls (assignments stay active).
13. `exposure` (optional, `max_exposure_cents[mode] > 0`): open order cost + open positions
    + cost.
14. `buying_power` (live, step 5): the probed figure minus live reservations and minus
    what live fills spent since the probe.
Pass: `ledger reserve`, `orders` row `approved`. Fail: `orders` row `rejected` with the
reason (also logged in `order_events`). The request body's limit fields, if any, are
ignored. Response `{"status": "approved"|"rejected", "order_id", "reason"}`.

## Executor and paper fills (`host/exchange/executor.py`, `host/exchange/paper.py`)

Every 250 ms: `SELECT ... FROM orders WHERE status='approved' AND NOT killed FOR UPDATE SKIP
LOCKED` -> `submitting` (commit) -> gateway `place` -> `open` with the exchange order id;
timeout -> stays `submitting` and is reconciled by client id, never resubmitted blind.
Paper gateway: `place` marks `open` immediately; fills come from `paper.simulate(order,
snapshot)` for every snapshot newer than the order's submission: walk ask levels with
`price <= order.price`, fill `min(remaining, participation * level_size)` per level,
fee per contract from `fee_model`; `fills` rows, `ledger fill`, order `partial`/`filled`.
One snapshot's level offers `participation * size` contracts to all paper orders on the
market together (in submission order), not to each order separately. Resting bids (the
first snapshot after submission had its ask above the price) fill only when a later
snapshot's ask crosses them, and then at the order's own limit price, as a resting limit
order does on a real book; a marketable order takes the ask levels as they are. Fees are
rounded on the order's cumulative filled size so per-fill roundings never exceed the
fee reserved at approval; every cost is rounded half up in one place
(`orders.fill_cost_cents`), and what a fill consumed is read back from the ledger.
Nothing fills while the kill switch is on.
`cancel_requested` -> `cancelled` plus `ledger release` of the unfilled part (paper:
immediate, done by whoever requested it, host or exchange; live: by the exchange with
retry 1,2,4,8 s forever, after its fills were read). Orders expire at `gtd_seconds`
after submission (paper at once; a live row through the cancel path, closing as
`expired` once the exchange no longer lists it). With
`trade_pregame_only`, kickoff is a hard cutoff: `gtd_at` is capped at the game's
`kickoff_at`, an executor pass cancels every active order of a game that has kicked off
(actor `exchange`, reason `kickoff`; live rows become `cancel_requested`), and the paper
simulator never uses a snapshot taken at or after kickoff, so no in-game book can fill a
pregame order and CLV is always measured against the pre-kickoff closing price.

## Kill switch (full)

`POST /api/kill` in one transaction under both approval locks: `kill_switch=true`,
`live_enabled=false`; `UPDATE orders SET status = CASE WHEN status='approved' THEN
'cancelled' WHEN mode='paper' THEN 'cancelled' ELSE 'cancel_requested' END WHERE status IN
('approved','submitting','open','partial')` with `ledger release` for every row that
became `cancelled`; assignments `active -> halted`; audit. Heartbeats carry `kill=true`;
trade workers stop proposing. Live `cancel_requested` rows are cancelled by the exchange
with retry until `open_orders()` is empty (step 5). Reset (`RESUME`) clears the flag only;
assignments stay halted and `/trading` offers "Activate all paper". A kill works with every
worker offline and, for paper, with the exchange down. `kill_switch` is read-only for
`POST /api/settings` (400: use `/api/kill` or `/api/kill/reset`), and `live_enabled`
cannot be switched on while the fleet is killed. Activate and "Activate all paper" read
the flag `FOR SHARE` before touching an assignment, so a kill committing at the same time
is waited for and nothing it halted is re-activated.

## Role switch away from trade, orphans

Before acknowledging a role change away from `trade`, the agent calls
`POST /api/v1/trade/release` with its lease tokens: the host cancels that worker's open
orders (paper immediately; live `cancel_requested`, waiting up to 3 s for the exchange to
confirm), releases the trade jobs to `queued` (another trade worker will claim them), and
answers `{"cancelled": n, "pending": m}`. The agent then sends its ack heartbeat. The host
loop's orphan rule: a worker silent for `orphan_cancel_after_s` with open orders gets them
cancelled the same way; its trade jobs follow the normal lease expiry.

## Settlement, bets, scoring, eligibility (`host/exchange/settle.py`)

Finals: on game days the exchange polls the ESPN scoreboard (`settings.scores_url`) every
60 s for games with assignments that have kicked off; a completed game updates
`games` (scores, status `final`, `raw.score_source = "espn"`); the nflverse refresh confirms
later. With `market_source = sim`, `host.cli simulate-final <game_id> --home N --away M`
sets a final for testing (refused for other sources unless `FLEET_DEV`).
On final, for every assignment of the game not yet settled: resolve the markets (winner
YES, loser NO; tie: push), settle positions (`ledger settle`), cancel open orders with
release, write one `bets` row per filled order (entry = VWAP, fee, cost, my_p, market_p,
edge, stake, `closing_price`, `clv = closing_price - entry_price`, result win/loss/push,
pnl), upsert `model_scores (model_id, game_id, mode)` with `n_bets, stake_cents, pnl_cents,
avg_clv` (stake-weighted), mark the assignment `settled`, complete the trade job
(`succeeded` with the score summary), then recompute lineage eligibility:
`paper_ok -> live_eligible` when the lineage's pooled paper scores have `games >=
min_games, bets >= min_bets, days since first paper bet >= min_days, avg_clv >= min_clv,
pnl >= min_pnl_cents` (`settings.thresholds_paper`); `games` counts distinct games (one
game traded by several models of the lineage counts once; a settled game with no bet
still counts), the rest are summed over the lineage's `model_scores`; a lineage that stops
meeting them drops back to `paper_ok` (live assignments halted, orders cancelled), and so
does every other way out of `live_eligible` (a backtest thresholds change, a new backtest
result, a retirement). No override exists. Settlement runs under both approval locks and locks a game's open
orders before its bankrolls (the order paper fills and the kill use), so a kill pressed
mid-settlement waits instead of deadlocking.

## Leaderboard and P&L

Leaderboard rows gain paper (and later live) columns from `model_scores` pooled per
lineage: games, bets, P&L, ROI, avg CLV. Rank mode: paper when a lineage has `>= 5` paper
games and `>= 30` paper bets, by shrunk CLV `avg_clv * bets / (bets + 25)` (tie-break ROI);
otherwise the step 3 backtest ranking. `GET /api/pnl` becomes real: today = `bets.pnl`
settled today (owner tz) + mark-to-mid change of open positions since the later of day
start and entry; all-time = all settled `bets.pnl` + unrealized; per worker = the same
restricted to orders that worker requested; per mode.

## Dashboard additions (`/trading`, see docs/DASHBOARD.md)

- Top bar: P&L per mode ("paper today $x · all $y"; live appears in step 5), banners:
  "EXCHANGE DOWN" when `exchange_state.heartbeat_at` is older than 15 s while any order is
  open or the kill switch is on; "N assignments unattended" when a trade job has been
  queued > 60 s with an active assignment.
- `/trading`: assignments table (game, kickoff, model, mode, status, bankroll
  available/reserved/open/realized, open orders, actions: halt, activate, settle-now when
  final); create-assignment form (upcoming games with confirmed markets, models of
  non-retired lineages, mode paper, bankroll dollars defaulting to
  `default_bankroll_cents`); "Activate all paper" after a kill reset; open orders (cancel
  button); last 50 orders with reject reasons and rationale; fills; unmatched markets with a
  link-to-game form; snapshot ages per market; exchange state (heartbeat age, source, last
  error); ledger replay status.
- Fleet cards: today's P&L per worker. Models page: Assign enabled (opens the form
  prefilled). Settings: a Trading group with every new key.

## Rate limits

The exchange process keeps one token bucket per category from `settings.rate_limits`
(`orders_per_s, cancels_per_s, market_data_per_s, account_per_s`), fleet-wide by
construction. Paper orders take no tokens. Priority: cancels, orders, fills, snapshots.
A 429 (`RateLimited`, raised by every source on that status) halves the refill rate for
60 s. Book fetches time out after 2 s and a snapshot pass stops fetching after 5 s of
wall clock (the rest is due again next pass); each snapshot is stamped with the time its
fetch returned, not the pass start. Failed book fetches and a game whose settlement
rolled back are reported as the exchange's `last_error` (the heartbeat carries it to
`/trading`) even though the loop carries on.

## Tests that must exist

`tests/test_limits.py`: approves_within_limits; rejects_over_max_bet;
max_bet_counts_open_same_market_orders; assignment_max_bet_lowers_never_raises_global;
rejects_cost_over_available; one_live_per_game_and_n_paper; rejects_when_losses_today_plus_cost_exceed_daily_loss;
daily_loss_per_mode; daily_loss_owner_tz_boundary; live_daily_trip_halts_live_only_paper_continues;
exposure_off_by_default_enforced_when_set; rejects_below_floor_cited_or_newest;
rejects_stale_book; rejects_participation_cap; rejects_unconfirmed_mapping; rejects_after_kickoff;
rejects_live_when_switch_off; rejects_live_when_lineage_not_eligible; rejects_when_worker_role_not_trade;
rejects_when_job_preempted_or_cancel_requested; rejects_stale_lease_token;
mode_from_assignment_not_request; payload_limit_fields_ignored; price_out_of_band;
concurrent_requests_cannot_oversubscribe_bankroll (10 threads, exactly one approved);
concurrent_approvals_cannot_exceed_daily_loss_across_games; rejections_persisted_with_reason;
ledger_append_only_trigger; ledger_replay_matches_columns_with_open_positions.
`tests/test_kill_switch.py` (extend): kill_sets_flag_and_disables_live;
kill_rejects_new_requests_first; approved_unsubmitted_go_straight_to_cancelled_and_release_reserve;
paper_open_orders_cancelled_in_the_kill_transaction; live_open_orders_become_cancel_requested;
kill_update_is_scoped (filled/rejected untouched); executor_racing_kill_cannot_open;
executor_never_submits_while_killed; kill_vs_approval_race (20 threads, zero approved/open after);
kill_during_submitting_cancels_by_client_id; live_cancel_retries_until_confirmed;
heartbeat_carries_kill; kill_idempotent_and_persists_across_restart; cli_kill_works_without_api;
exchange_down_banner_after_15s; reset_requires_exact_RESUME; reset_keeps_assignments_halted;
every_status_change_writes_order_event; trade_worker_stops_proposing_under_kill;
role_switch_away_from_trade_cancels_first.
Plus `test_paper_fills.py`, `test_settlement.py` (closing price, CLV sign, push, scores,
eligibility paper gate and demotion), `test_exchange.py` (executor outbox, snapshots
cadence, retention/bars, rate limiter, heartbeat, orphan rule), `test_adapters.py` (sim
determinism; polymarket_us and polymarket_clob parsers against fixture JSON, including
malformed payloads), `test_trade_worker.py` (tick maths, proposals, cancels, kill, release
handshake), dashboard tests for `/trading`, and the e2e extension.

## Selling (step 6 Part B): mark-to-model

The buy rule gets its mirror image. Orders gain `side` (`buy` | `sell`, migration
`0008_sells.sql`); every existing order is a buy. On each tick, for every market where
the assignment holds contracts:
- `p_side` from the model; `proceeds = bid`; `fee = taker_rate * bid * (1 - bid)`;
  `sell_edge = bid - fee - p_side`. If `sell_edge >= min_edge`, propose a limit SELL at the
  bid for `min(position size - open sell size, participation * bid depth at or above the
  price)` contracts. Never more than the position (no shorting); one open sell per market.
- Approval for sells (`host/trading/sells.py`): kill, lease, assignment active, market
  confirmed and unresolved, mode gate, stale book, participation (bid side), price band
  (`>= bid - 0.05`), size within the position; no reservation (`orders.cost_cents = 0`,
  no ledger row) and no daily-loss check, but the order and its events are logged like
  any other.

Money side, as built:

| Event | Ledger kind | d_available | d_reserved | d_open | d_realized | fills.basis_cents |
|---|---|---|---|---|---|---|
| buy approved | `reserve` | `-cost` | `+cost` | 0 | 0 | |
| buy fill | `fill` | 0 | `-(notional + fee)` | `+notional` | `-fee` | `notional` |
| sell approved | none | | | | | |
| sell fill | `sell` | `+(proceeds - fee)` | 0 | `-basis` | `+(proceeds - fee - basis)` | `basis` |
| sell cancelled, expired or killed | none (nothing reserved) | | | | | |
| settlement | `settle` | `+payout` | 0 | `-remaining basis` | `+(payout - remaining basis)` | |

`notional = proceeds = price * size * 100` rounded half up (`orders.fill_cost_cents`).

- Positions (`host/trading/positions.py`) are signed sums over fills: `size = bought -
  sold`, `basis_cents = sum(buy basis) - sum(sell basis)`, `avg_cost = basis_cents /
  (size * 100)`; rows `{"market_id", "side", "size", "basis_cents", "avg_cost"}` plus
  the mark fields. A position sold down to zero disappears.
- Sell basis (`positions.sell_basis_cents`): `remaining basis * size / remaining size`
  rounded half up to the cent, and exactly the remaining basis when the sale closes the
  position, so a position sold in any number of steps ends at zero open cost. A sell
  fill larger than the position is refused (`orders.record_fill` raises; the live path
  then auto-kills `late_fill` as for any fill the books cannot take). The bankroll row
  is locked before the position is read.
- Paper fills for sells (`host/exchange/paper.py`) walk the bid levels at or above the
  limit with `fleet.sim.book.walk(side="sell")` and the same participation rule; a sell
  whose first snapshot after submission had its bid below the limit rests and fills at
  its limit when a later bid crosses. Each snapshot level is shared by all paper orders
  on the market per side: bid levels among sells, ask levels among buys. Paper fill ids
  are `paper:{order}:{snapshot}:b{level}` for bid levels (ask levels keep
  `paper:{order}:{snapshot}:{level}`). A paper sell never fills more than the
  assignment holds.
- Live sells go through the same gateway: `market_source_config.polymarket_us.live`
  gains `side_sell` (default `"SELL"`), sent for a sell order (`side_buy` for a buy); a
  null side refuses the place (`NotConfigured`, rejected by the exchange). Live fills
  of sells are booked by the same `record_fill`.
- Kill and cancels: a kill cancels open sells exactly like buys (paper at once, live
  `cancel_requested`); there is nothing to release. Positions stay.
- Settlement (`host/exchange/settle_sells.py`): a filled sell order gets its own `bets`
  row (`order_side` sell, `result` sold, `entry_price` = average sell price,
  `cost_cents` = basis sold, `stake_cents` 0, `pnl_cents` = proceeds - fee - basis,
  `clv` null). Each filled buy row covers only the contracts still held: per market the
  remaining basis is split over the buy rows by their bought basis and the payout by
  their bought contracts, floor shares with the rounding residual on the last row, so
  the rows add up to the ledger `settle` row exactly; `pnl = payout - basis - buy fee`,
  `stake_cents` stays the whole bought basis plus fee, CLV against the buy VWAP. A
  fully sold position settles with no `settle` row and its buy rows carry only their
  fees. `model_scores.pnl` includes sells; `n_bets` counts buys; CLV stays
  stake-weighted over buys. Bets P&L summed per assignment equals its ledger realized.
- P&L (`host/pnl.py`): before settlement a sell fill counts `proceeds - fee - mark` of
  its contracts (today: the same when it filled today, else `-(mark - start mark)`), so
  open P&L is the realized gain of the sales plus the mark-to-mid of what is still
  held; after settlement the `sold` bets row carries it.
- Dashboard: sells shown with a `sell` chip and their realized P&L; positions show size,
  average cost, current bid and unrealized P&L.
- Tests: `tests/test_sell_ledger.py` (sell maths by hand, ledger invariants after
  partial and full sales, no shorting, P&L), `tests/test_sell_paper.py` (marketable and
  resting sells, shared participation with a buy on the same snapshot),
  `tests/test_sell_settlement.py` (exact totals after a partial sale, loss, push, a full
  sale), `tests/test_sell_kill_live.py` (kill cancels open sells, the live gateway and
  executor send SELL, a live sell fill); the worker rule and the approval have their
  own tests.

# Models, backtester, search and training (step 3)

Everything here runs on workers in pure Python (standard library only) and is
deterministic for a given seed. Money is integer cents. Probabilities are floats in (0, 1).

## Data

`games` rows come from nflverse `games.csv` (CC BY 4.0; show attribution in the README and
the Models page). The host ingests it and serves it to workers (`GET /api/v1/data/games`).
Fields used by models: `game_id, season, game_type (REG|POST), week, kickoff_at (UTC),
home_team, away_team, home_score, away_score, home_moneyline, away_moneyline, spread_line,
total_line, home_rest, away_rest, div_game, roof, surface, temp, wind`. Team codes are
normalised for continuity: `OAK -> LV`, `SD -> LAC`, `STL -> LA`. Games without both
moneylines are used for Elo updates but never bet on or scored.

Odds: American `o` to implied probability: `|o| / (|o| + 100)` when `o < 0`, else
`100 / (o + 100)`. Devig: `p_home_market = imp_home / (imp_home + imp_away)`.

## Model interface (`fleet/models/base.py`)

```python
class Model:
    family: str                     # registry key, e.g. "elo_blend"
    params: dict                    # hyperparameters (JSON)
    def fit(self, games, through, should_stop) -> None   # games sorted by kickoff; `through`
                                                          # = (season, week) inclusive, or None
    def predict(self, game, market_p, features) -> float # P(home win) given the current
                                                          # market price and a features dict
    def to_json(self) -> dict        # artifact (ratings, coefficients, through); no pickle
    @classmethod
    def from_json(cls, params, artifact) -> "Model"
    @staticmethod
    def search_space(rng) -> dict    # one random params draw for model_search
    @staticmethod
    def summary(params, metrics) -> str   # exactly three sentences, plain text
```
`fleet/models/registry.py`: `FAMILIES = {"elo_blend": EloBlend}`. `features` is a dict
with `home_rest, away_rest, div_game, roof, surface, temp, wind, week, season, game_type`.

## First family: `elo_blend`

Elo: every team starts at 1500 (1999). Per game, expected home win probability
`e = 1 / (1 + 10 ** (-(r_home + hfa + rest_adj - r_away) / 400))` where
`rest_adj = rest_per_day * clamp(home_rest - away_rest, -3, 3)`. After the game, with
`s = 1 (home win), 0.5 (tie), 0` and margin `m = |home_score - away_score|`:
`mult = 1 if mov_scale == 0 else mov_scale * ln(max(m, 1) + 1) * 2.2 / (0.001 * d + 2.2)` where
`d` is the winner's pre-game rating advantage including hfa (0 for ties); the margin is
floored at 1 (as in the 538 reference) so a tie still moves the ratings by `k * ln 2 * (0.5 - e)`;
`delta = k * mult * (s - e)`; `r_home += delta; r_away -= delta`. At each new season every
rating regresses: `r = 1500 + (r - 1500) * (1 - regress)`.

Blend: `logit(p) = a * logit(p_elo) + b * logit(p_market) + c`, with `a, b, c` fitted by
Newton's method on the log-loss of the training games (L2 penalty 1e-3 on a and b, at most
25 iterations, tolerance 1e-8; deterministic). `p_elo` for a training game is the Elo
expectation before that game. The fit uses the finished games with moneylines up to and
including the `through` week (the same window the Elo replay sees), so a child trained
through `(S, 22)` carries exactly the blend the backtest fold testing `S + 1` uses.

Params and search space (uniform draws unless noted):
`k [10, 40]`, `hfa [20, 90]`, `regress [0.1, 0.6]`, `rest_per_day [0, 4]`,
`mov_scale {0, 1}` (coin flip), `min_edge [0.01, 0.08]`, `kelly_fraction [0.1, 0.5]`.
`min_edge` and `kelly_fraction` do not change predictions, only the betting rule.
`params_hash` = sha256 of the canonical JSON (sorted keys, 6 decimals), first 16 hex.

Artifact: `{"ratings": {team: float}, "blend": {"a", "b", "c"}, "through": [season, week],
"season": s, "games_seen": n}`. `season` is the season the ratings sit in (the last one
actually replayed, which is before `through[0]` when training through a season that has no
games yet), so a reloaded model applies the between-season regression exactly once;
`from_json` falls back to `through[0]` for an artifact without it. `games_seen` counts
played games only (a schedule row without scores is not learned from).

## Backtest (`fleet/sim/backtest.py`)

Walk-forward by season over `seasons = [first, last]` (settings `backtest_seasons`,
default `[2010, <last complete season>]`). For each test season `S`:
1. Replay Elo from 1999 through the end of season `S - 1` (cheap; no stored state needed).
2. Fit the blend on all moneyline games in seasons `< S` (at least 3 seasons of history,
   otherwise skip `S`).
3. Walk season `S` in kickoff order: before each game compute `p = predict(game,
   p_market, features)`, apply the betting rule below, record the outcome, then update Elo
   with the result. The blend is not refitted inside the season.
A test season is one checkpoint unit (well under 3 s on an old i5).

Betting rule ("closing-line fill", the only price we have for history):
- For each side `s` in {home, away}: `price_s = p_market_s + half_spread`;
  `fee_s = taker_rate * price_s * (1 - price_s)`; `cost_s = price_s + fee_s`
  (cents per $1 contract; `fee_model` settings, defaults `taker_rate 0.05`, `half_spread 0.01`).
- `edge_s = p_model_s - cost_s`. Bet the side with the larger edge if `edge >= min_edge`;
  at most one bet per game.
- `bankroll` per game is `default_bankroll_cents` (reset every game). Stake =
  `floor(kelly_fraction * bankroll * edge / (1 - cost))`, capped at `max_bet_cents` and the
  bankroll; contracts = `stake / cost` (float); payout = `contracts * 100` cents if the side
  wins (ties: stake returned), else 0; `pnl = payout - stake`, rounded to cents.
- CLV is 0 by construction here (entry at the close). Backtests on sportsbook closing lines
  measure calibration and discipline, not true edge; the paper phase on live Polymarket
  prices (step 4) is where CLV is measured.

Metrics (whole backtest and per season): `n_games, n_bets, total_stake_cents, pnl_cents,
roi = pnl / total_stake (0 if no bets), hit_rate, avg_edge, avg_stake_cents, log_loss`
(model `p` vs outcome over every moneyline game, ties count 0.5), `brier, market_log_loss`
(same for `p_market`), `calibration` (10 buckets of `p`: count, mean p, mean outcome),
`max_drawdown_cents` (largest peak-to-trough of cumulative pnl in game order),
`max_drawdown = max_drawdown_cents / (default_bankroll_cents * trade_max_games)` (the
capital one trade worker needs; `null` when that capital is 0, for example while trading is
paused with `trade_max_games 0`, so it never reads as a 0% drawdown and never passes the
eligibility gate), `seasons` (list), `blend` (the fitted `{a, b, c}` of that
season's fold; the whole-backtest value is the last fold's, so `summary` can state the
closing-line weight from params and metrics alone). `n_games` counts scored games (both
moneylines present). Deterministic: no randomness anywhere.

## Model search (`fleet/sim/search.py`)

Params: `family, n (default 200), seed, seasons [first, last], top_k (default 5)`.
Candidate `i` draws its params from `random.Random(f"{seed}:{i}")` so resume is exact.
Each candidate runs the full walk-forward backtest; unit = candidate x test season.
Objective: `score = roi * n_bets / (n_bets + 100)` (shrunk ROI), ties broken by lower
`log_loss`. Checkpoint: `{"next": [i, season_index], "current": {partial per-season
metrics of candidate i}, "top": [best candidates so far with params and metrics],
"evaluated": i}`. Result: `{"evaluated": n, "seasons": [...], "top": [...],
"create_models": [{"family", "params", "artifact": null, "backtest_metrics", "summary",
"trained_through": null}]}`; the agent turns `create_models` into `models` rows (see
PROTOCOL step 3) and replaces them with ids in the result it completes with.

## Training (`fleet/sim/train.py`)

Params: `model_id, through {"season", "week"}`. The agent injects the parent model
(params + lineage) into the job context. The job replays Elo through the `through` point
with the parent's params, fits the blend on the finished moneyline games up to and
including it, and returns `create_models: [{"family", "params" (the parent's, verbatim, so
the child keeps the lineage's `params_hash`), "artifact", "parent_model_id",
"trained_through": [season, week]}]`. The host puts the child in the parent's lineage with the lineage's status
and backtest metrics. Unit = one season of Elo replay.

## Summary (three sentences, template per family)

1. What it is: "Elo blend (K 24, home edge 55, 71% weight on the closing line, margin
   scaling on)." The weight is `b / (a + b)` of the fitted blend; when `a <= 0` it reads
   "all weight on the closing line, Elo adds nothing" instead of a percentage.
2. How it bet in the backtest: "Across 2010-2025 it placed 312 bets at an average edge of
   3.4% and returned +2.1% on stake with a 14% max drawdown." A single season reads
   "Across 2019"; with no bets the sentence is "Across 2010-2025 it never found an edge
   above its 3.0% minimum after fees, so it placed no bets." (never "+0.0% on stake"); with
   fewer than 50 bets (the leaderboard gate) it ends "... max drawdown (too few bets to
   judge)."; a null drawdown reads "an unknown max drawdown".
3. Calibration: "Log-loss 0.662 against the market's 0.659; it leans on the market and adds
   little, so treat the edge as unproven until paper trading shows positive CLV." It says
   "it beats the closing line on calibration" only when the log-loss is more than 0.002
   below the market's and (step 6) the era's market test gives p < 0.05; ahead by that
   margin with a larger p it reads "it is ahead of the closing line but not significantly
   (market test p 0.17), so treat the edge as unproven".
The owner can edit the text on the Models page.

## Eligibility (host, `host/eligibility.py`)

Per lineage, recomputed whenever a model row is created or its backtest metrics change:
`candidate -> paper_ok` when the root model's backtest has `n_bets >= min_bets`,
`roi >= min_roi` and `max_drawdown <= max_drawdown` (settings `thresholds_backtest`,
defaults `{"min_bets": 200, "min_roi": 0.02, "max_drawdown": 0.30}`); otherwise it stays
or returns to `candidate`. `paper_ok -> live_eligible` (and back) is decided after every
settlement from the lineage's pooled paper record against `thresholds_paper`
(docs/TRADING.md, "Settlement, bets, scoring, eligibility"). Status is held on every row
of the lineage.

## Leaderboard (step 3 scope)

One row per lineage, using the root model's backtest metrics: rank by shrunk ROI
(`roi * n_bets / (n_bets + 100)`), tie-break log-loss; ranked only if `n_bets >= 50`,
otherwise listed below as unranked. Columns: status, family, short params, ROI, bets,
log-loss vs market, max drawdown, seasons, summary. Step 4 adds the paper (and later
live) record per lineage and the paper rank mode (docs/TRADING.md, "Leaderboard and P&L").

-- Step 3: nflverse games, model rows (lineages, eligibility) and the settings behind
-- the backtester. See docs/MODELS.md and the "Step 3 additions" of docs/PROTOCOL.md.

CREATE TABLE games (
  game_id         text PRIMARY KEY,
  season          integer NOT NULL,
  game_type       text NOT NULL,
  week            integer NOT NULL,
  gameday         date NOT NULL,
  gametime        text,
  kickoff_at      timestamptz,
  home_team       text NOT NULL,
  away_team       text NOT NULL,
  home_score      integer,
  away_score      integer,
  home_moneyline  integer,
  away_moneyline  integer,
  spread_line     real,
  total_line      real,
  home_rest       integer,
  away_rest       integer,
  div_game        boolean NOT NULL DEFAULT false,
  roof            text,
  surface         text,
  temp            integer,
  wind            integer,
  status          text NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'final')),
  raw             jsonb NOT NULL,
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX games_season_week_idx ON games (season, week);
CREATE INDEX games_kickoff_idx ON games (kickoff_at);

CREATE TABLE models (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  lineage_id        uuid NOT NULL,
  family            text NOT NULL,
  params            jsonb NOT NULL DEFAULT '{}'::jsonb,
  params_hash       text NOT NULL,
  artifact          jsonb,
  parent_model_id   uuid REFERENCES models(id),
  trained_through   jsonb,
  summary           text,
  status            text NOT NULL DEFAULT 'candidate'
                    CHECK (status IN ('candidate', 'paper_ok', 'live_eligible', 'retired')),
  backtest_metrics  jsonb,
  created_by_job_id uuid REFERENCES jobs(id),
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);
-- Idempotent creation: one row per (family, params, training point).
CREATE UNIQUE INDEX models_identity_idx ON models (family, params_hash, COALESCE(trained_through::text, ''));
CREATE INDEX models_lineage_idx ON models (lineage_id);

INSERT INTO settings (key, value) VALUES
  ('fee_model',              '{"taker_rate": 0.05, "half_spread": 0.01}'),
  ('thresholds_backtest',    '{"min_bets": 200, "min_roi": 0.02, "max_drawdown": 0.3}'),
  ('backtest_seasons',       '[2010, null]'),
  ('nflverse_refresh_hours', '6'),
  ('nflverse_url',           '"https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"');

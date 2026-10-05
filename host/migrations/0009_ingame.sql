-- Step 6 Part C: in-game trading (docs/INGAME.md). A live game-state feed (ESPN, with
-- Yahoo as an opt-in cross-check), the measured lag of each feed against the market,
-- the play-by-play rows the in-game model trains on, and the in-game flags on
-- assignments, orders, bets and scores. In-game orders are paper-only in this step.

-- The ESPN event id lives in games.raw (nflverse `espn` column); index it for the feed.
CREATE INDEX games_espn_idx ON games ((raw->>'espn'));

CREATE TABLE game_state (
  id             bigserial PRIMARY KEY,
  game_id        text NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
  ts             timestamptz NOT NULL DEFAULT now(),
  source         text NOT NULL CHECK (source IN ('espn_summary', 'espn_scoreboard', 'yahoo')),
  event_ts       timestamptz,
  status         text NOT NULL CHECK (status IN ('pre', 'in', 'half', 'end_period', 'final')),
  period         integer,
  clock_seconds  integer,
  home_score     integer,
  away_score     integer,
  possession     text CHECK (possession IN ('home', 'away')),
  down           integer,
  distance       integer,
  yardline_100   integer,
  home_timeouts  integer,
  away_timeouts  integer,
  play_id        text,
  play_text      text,
  raw            jsonb
);
CREATE INDEX game_state_game_ts_idx ON game_state (game_id, ts DESC);
CREATE UNIQUE INDEX game_state_play_idx ON game_state (game_id, source, play_id) WHERE play_id IS NOT NULL;

CREATE TABLE feed_lag (
  id               bigserial PRIMARY KEY,
  game_id          text NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
  event_kind       text NOT NULL CHECK (event_kind IN ('score', 'possession')),
  event_key        text NOT NULL,
  event_ts         timestamptz,
  source           text NOT NULL,
  feed_seen_at     timestamptz NOT NULL,
  market_moved_at  timestamptz,
  lag_s            real,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (game_id, event_key, source)
);
CREATE INDEX feed_lag_source_idx ON feed_lag (source, feed_seen_at DESC);

CREATE TABLE pbp_rows (
  game_id            text NOT NULL,
  play_id            text NOT NULL,
  season             integer NOT NULL,
  home_win           real,
  score_diff         integer NOT NULL,
  seconds_remaining  integer NOT NULL,
  half               integer NOT NULL,
  down               integer,
  ydstogo            integer,
  yardline_100       integer,
  posteam_is_home    boolean,
  home_timeouts      integer,
  away_timeouts      integer,
  pregame_p_home     real,
  vegas_wp           real,
  PRIMARY KEY (game_id, play_id)
);
CREATE INDEX pbp_rows_season_idx ON pbp_rows (season);

ALTER TABLE assignments
  ADD COLUMN ingame_model_id uuid REFERENCES models(id),
  ADD COLUMN trade_ingame    boolean NOT NULL DEFAULT false;

ALTER TABLE orders ADD COLUMN ingame boolean NOT NULL DEFAULT false;

ALTER TABLE bets
  ADD COLUMN ingame          boolean NOT NULL DEFAULT false,
  ADD COLUMN state_at_entry  jsonb;

ALTER TABLE model_scores
  ADD COLUMN ingame_n_bets     integer NOT NULL DEFAULT 0,
  ADD COLUMN ingame_pnl_cents  bigint NOT NULL DEFAULT 0;

INSERT INTO settings (key, value) VALUES
  ('trade_ingame',            'false'),
  ('ingame_tick_s',           '5'),
  ('ingame_max_state_age_s',  '30'),
  ('ingame_quiet_seconds',    '20'),
  ('ingame_cutoff_seconds',   '120'),
  ('ingame_dead_zone',        '0.03'),
  ('ingame_min_edge',         '0.05'),
  ('ingame_max_bet_cents',    '500'),
  ('ingame_gtd_seconds',      '60'),
  ('ingame_max_lag_s',        '20'),
  ('ingame_lag_min_events',   '5'),
  ('gamestate_poll_s',        '4'),
  ('gamestate_max_rps',       '1.0'),
  ('gamestate_sources',       '["espn"]'),
  ('espn_summary_url',        '"https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={event_id}"'),
  ('yahoo_pbp_url',           '""'),
  ('yahoo_poll_s',            '12');

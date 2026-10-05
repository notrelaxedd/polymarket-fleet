-- Step 6 Part B: richer signals and snapshot replay (docs/ROBUSTNESS.md Part B).
-- Injury reports and per team-game play-by-play aggregates from nflverse (free), the
-- snapshot-replay metrics of a lineage (kept apart from the closing-line backtest
-- metrics, never overwriting them), and the settings the replay and the ingest read.

CREATE TABLE injuries (
  season         integer NOT NULL,
  game_type      text NOT NULL,
  week           integer NOT NULL,
  team           text NOT NULL,
  gsis_id        text NOT NULL,
  full_name      text,
  position       text,
  report_status  text,
  date_modified  timestamptz,
  updated_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (season, game_type, week, team, gsis_id)
);
CREATE INDEX injuries_team_week_idx ON injuries (season, week, team);

CREATE TABLE team_game_stats (
  game_id           text NOT NULL,
  team              text NOT NULL,
  season            integer NOT NULL,
  week              integer NOT NULL,
  kickoff_at        timestamptz,
  off_epa_per_play  double precision,
  def_epa_per_play  double precision,
  pass_rate         double precision,
  plays             integer NOT NULL DEFAULT 0,
  success_rate      double precision,
  updated_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (game_id, team)
);
CREATE INDEX team_game_stats_team_kickoff_idx ON team_game_stats (team, kickoff_at);

ALTER TABLE models ADD COLUMN snapshot_metrics jsonb;

INSERT INTO settings (key, value) VALUES
  ('allow_sim_prices',                'false'),
  ('decision_minutes_before_kickoff', '60'),
  ('nflverse_injuries_url',           '"https://github.com/nflverse/nflverse-data/releases/download/injuries/injuries_{season}.csv"'),
  ('nflverse_pbp_url',                '"https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.csv.gz"'),
  ('signals_refresh_hours',           '24');

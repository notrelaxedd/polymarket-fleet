-- Step 6 Part A: the held-out validation era, confidence intervals and stress tests
-- (docs/ROBUSTNESS.md). Validation and stress metrics live on every row of a lineage
-- like the backtest metrics; the paper CLV bootstrap of a lineage is cached per
-- lineage so the leaderboard can show it without recomputing.

ALTER TABLE models
  ADD COLUMN validation_metrics jsonb,
  ADD COLUMN stress_metrics     jsonb;

CREATE TABLE lineage_paper_ci (
  lineage_id   uuid PRIMARY KEY,
  n_bets       integer NOT NULL,
  avg_clv      real,
  clv_low      real,
  clv_high     real,
  computed_at  timestamptz NOT NULL DEFAULT now()
);

INSERT INTO settings (key, value) VALUES
  ('validation_seasons', '[2022, null]'),
  ('search_workers',     '"auto"');

-- The stricter gates (ROBUSTNESS.md A4): the validation era is the one judged, its
-- 5th percentile ROI must not be negative, the market test must be at least
-- suggestive, and an overfit or fragile model never trades paper money.
UPDATE settings
   SET value = '{"min_bets": 50, "min_roi": 0.02, "max_drawdown": 0.3, "require_validation": true,
                 "min_roi_ci_low": 0.0, "max_market_p": 0.1, "forbid_flags": ["overfit", "fragile"]}',
       updated_at = now()
 WHERE key = 'thresholds_backtest';

UPDATE settings
   SET value = value || '{"clv_ci_excludes_zero": true}'::jsonb, updated_at = now()
 WHERE key = 'thresholds_paper';

-- The search era ends before the validation era (the step 3 default ran to the last
-- complete season); an owner's own range is left alone.
UPDATE settings SET value = '[2010, 2021]', updated_at = now()
 WHERE key = 'backtest_seasons' AND value = '[2010, null]'::jsonb;

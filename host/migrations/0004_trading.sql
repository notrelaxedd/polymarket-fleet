-- Step 4: markets, snapshots, assignments, bankrolls, ledger, orders, fills, bets,
-- scores and the exchange state row. See docs/TRADING.md.

CREATE TABLE exchange_state (
  id                  boolean PRIMARY KEY DEFAULT true CHECK (id),
  heartbeat_at        timestamptz,
  market_source       text,
  auth_ok             boolean NOT NULL DEFAULT false,
  auth_checked_at     timestamptz,
  balance_cents       bigint,
  buying_power_cents  bigint,
  balance_checked_at  timestamptz,
  clock_skew_ms       integer,
  last_error          text,
  updated_at          timestamptz NOT NULL DEFAULT now()
);
INSERT INTO exchange_state (id) VALUES (true);

CREATE TABLE markets (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  platform            text NOT NULL,
  market_ref          text NOT NULL,
  event_ref           text,
  title               text NOT NULL,
  game_id             text REFERENCES games(game_id),
  side                text CHECK (side IN ('home', 'away')),
  mapping_confirmed   boolean NOT NULL DEFAULT false,
  mapping_confidence  real,
  status              text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed', 'resolved')),
  resolved_yes        boolean,
  tick                numeric(6,4) NOT NULL DEFAULT 0.01,
  min_size            integer NOT NULL DEFAULT 1,
  best_bid            numeric(6,4),
  best_ask            numeric(6,4),
  liquidity_usd_cents bigint,
  closing_price       numeric(6,4),
  last_snapshot_at    timestamptz,
  raw                 jsonb,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (platform, market_ref)
);
CREATE INDEX markets_game_idx ON markets (game_id);

CREATE TABLE price_snapshots (
  id                  bigserial PRIMARY KEY,
  market_id           uuid NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
  ts                  timestamptz NOT NULL DEFAULT now(),
  bid                 numeric(6,4),
  ask                 numeric(6,4),
  mid                 numeric(6,4),
  bid_depth           jsonb,
  ask_depth           jsonb,
  liquidity_usd_cents bigint
);
CREATE INDEX price_snapshots_market_ts_idx ON price_snapshots (market_id, ts DESC);
CREATE INDEX price_snapshots_ts_idx ON price_snapshots (ts);

CREATE TABLE price_bars (
  market_id               uuid NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
  minute                  timestamptz NOT NULL,
  open                    numeric(6,4), high numeric(6,4), low numeric(6,4), close numeric(6,4),
  bid                     numeric(6,4), ask numeric(6,4),
  min_liquidity_usd_cents bigint,
  n                       integer NOT NULL,
  PRIMARY KEY (market_id, minute)
);

CREATE TABLE assignments (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  game_id       text NOT NULL REFERENCES games(game_id),
  model_id      uuid NOT NULL REFERENCES models(id),
  lineage_id    uuid NOT NULL,
  mode          text NOT NULL CHECK (mode IN ('paper', 'live')),
  job_id        uuid REFERENCES jobs(id),
  max_bet_cents bigint,
  status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'halted', 'settled', 'cancelled')),
  created_by    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  settled_at    timestamptz,
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX assignments_one_live_per_game_idx ON assignments (game_id)
  WHERE mode = 'live' AND status IN ('active', 'halted');
CREATE UNIQUE INDEX assignments_paper_model_game_idx ON assignments (game_id, model_id)
  WHERE mode = 'paper' AND status IN ('active', 'halted');
CREATE INDEX assignments_game_idx ON assignments (game_id);

CREATE TABLE bankrolls (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  assignment_id      uuid NOT NULL UNIQUE REFERENCES assignments(id),
  mode               text NOT NULL,
  initial_cents      bigint NOT NULL CHECK (initial_cents >= 0),
  available_cents    bigint NOT NULL CHECK (available_cents >= 0),
  reserved_cents     bigint NOT NULL DEFAULT 0 CHECK (reserved_cents >= 0),
  open_cost_cents    bigint NOT NULL DEFAULT 0 CHECK (open_cost_cents >= 0),
  realized_pnl_cents bigint NOT NULL DEFAULT 0,
  updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ledger (
  id          bigserial PRIMARY KEY,
  bankroll_id uuid NOT NULL REFERENCES bankrolls(id),
  ts          timestamptz NOT NULL DEFAULT clock_timestamp(),
  mode        text NOT NULL,
  kind        text NOT NULL CHECK (kind IN ('fund', 'reserve', 'release', 'fill', 'settle', 'adjust')),
  d_available bigint NOT NULL DEFAULT 0,
  d_reserved  bigint NOT NULL DEFAULT 0,
  d_open      bigint NOT NULL DEFAULT 0,
  d_realized  bigint NOT NULL DEFAULT 0,
  ref_type    text,
  ref_id      text,
  note        text
);
CREATE INDEX ledger_bankroll_idx ON ledger (bankroll_id, id);
CREATE INDEX ledger_ts_idx ON ledger (mode, ts);

CREATE FUNCTION ledger_immutable() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'ledger rows are append-only';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER ledger_no_update BEFORE UPDATE OR DELETE ON ledger
  FOR EACH ROW EXECUTE FUNCTION ledger_immutable();

CREATE TABLE orders (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  client_request_id text NOT NULL UNIQUE,
  assignment_id     uuid REFERENCES assignments(id),
  kind              text NOT NULL DEFAULT 'model' CHECK (kind IN ('model', 'smoke')),
  worker_id         text REFERENCES workers(id),
  job_id            uuid REFERENCES jobs(id),
  market_id         uuid NOT NULL REFERENCES markets(id),
  mode              text NOT NULL CHECK (mode IN ('paper', 'live')),
  price             numeric(6,4) NOT NULL,
  size              integer NOT NULL CHECK (size > 0),
  cost_cents        bigint NOT NULL CHECK (cost_cents >= 0),
  fee_cents_est     bigint NOT NULL DEFAULT 0,
  snapshot_id       bigint REFERENCES price_snapshots(id),
  status            text NOT NULL CHECK (status IN ('rejected', 'approved', 'submitting', 'open', 'partial',
                      'filled', 'cancel_requested', 'cancelled', 'rejected_by_exchange', 'expired')),
  reject_reason     text,
  exchange_order_id text,
  submitted_at      timestamptz,
  gtd_at            timestamptz,
  filled_size       integer NOT NULL DEFAULT 0,
  avg_fill_price    numeric(8,6),
  my_p              real,
  market_p          real,
  edge              real,
  rationale         text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX orders_active_idx ON orders (status)
  WHERE status IN ('approved', 'submitting', 'open', 'partial', 'cancel_requested');
CREATE INDEX orders_assignment_idx ON orders (assignment_id, created_at DESC);
CREATE INDEX orders_worker_idx ON orders (worker_id, created_at DESC);

CREATE TABLE order_events (
  id          bigserial PRIMARY KEY,
  order_id    uuid NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  ts          timestamptz NOT NULL DEFAULT clock_timestamp(),
  from_status text,
  to_status   text,
  actor       text,
  detail      jsonb
);
CREATE INDEX order_events_order_idx ON order_events (order_id, id);

CREATE TABLE fills (
  id               bigserial PRIMARY KEY,
  order_id         uuid NOT NULL REFERENCES orders(id),
  ts               timestamptz NOT NULL DEFAULT now(),
  price            numeric(6,4) NOT NULL,
  size             integer NOT NULL CHECK (size > 0),
  fee_cents        bigint NOT NULL DEFAULT 0,
  mode             text NOT NULL,
  exchange_fill_id text UNIQUE,
  snapshot_id      bigint
);
CREATE INDEX fills_order_idx ON fills (order_id, id);

CREATE TABLE bets (
  id            bigserial PRIMARY KEY,
  order_id      uuid NOT NULL UNIQUE REFERENCES orders(id),
  assignment_id uuid NOT NULL REFERENCES assignments(id),
  model_id      uuid NOT NULL REFERENCES models(id),
  lineage_id    uuid NOT NULL,
  game_id       text NOT NULL REFERENCES games(game_id),
  worker_id     text,
  mode          text NOT NULL,
  date          date NOT NULL,
  sport         text NOT NULL DEFAULT 'NFL',
  event         text NOT NULL,
  platform      text NOT NULL,
  contract      text NOT NULL,
  side          text NOT NULL,
  entry_price   numeric(8,6) NOT NULL,
  fee_cents     bigint NOT NULL DEFAULT 0,
  cost_cents    bigint NOT NULL,
  my_p          real,
  market_p      real,
  edge          real,
  stake_cents   bigint NOT NULL,
  closing_price numeric(6,4),
  clv           real,
  result        text NOT NULL CHECK (result IN ('win', 'loss', 'push')),
  pnl_cents     bigint NOT NULL,
  settled_at    timestamptz NOT NULL DEFAULT now(),
  notes         text
);
CREATE INDEX bets_lineage_idx ON bets (lineage_id, mode, settled_at);
CREATE INDEX bets_settled_idx ON bets (mode, settled_at);

CREATE TABLE model_scores (
  model_id    uuid NOT NULL REFERENCES models(id),
  game_id     text NOT NULL REFERENCES games(game_id),
  mode        text NOT NULL,
  lineage_id  uuid NOT NULL,
  n_bets      integer NOT NULL,
  stake_cents bigint NOT NULL,
  pnl_cents   bigint NOT NULL,
  avg_clv     real,
  computed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (model_id, game_id, mode)
);
CREATE INDEX model_scores_lineage_idx ON model_scores (lineage_id, mode);

INSERT INTO settings (key, value) VALUES
  ('participation',             '0.5'),
  ('book_max_age_s',            '60'),
  ('orphan_cancel_after_s',     '30'),
  ('gtd_seconds',               '900'),
  ('snapshot_retention_days',   '14'),
  ('snapshot_active_s',         '2'),
  ('snapshot_idle_s',           '30'),
  ('market_source',             '"sim"'),
  ('market_source_config',      '{"polymarket_us": {"base_url": "https://gateway.polymarket.us", "markets_path": "/v1/markets", "book_path": "/v1/markets/{market_ref}/book", "sport_query": "sport=nfl"}, "polymarket_clob": {"gamma_url": "https://gamma-api.polymarket.com", "clob_url": "https://clob.polymarket.com", "tag_slug": "nfl"}}'),
  ('market_lookahead_days',     '8'),
  ('max_paper_models_per_game', '3'),
  ('thresholds_paper',          '{"min_games": 10, "min_bets": 40, "min_days": 21, "min_clv": 0.0, "min_pnl_cents": 1}'),
  ('trade_pregame_only',        'true'),
  ('trade_tick_s',              '5'),
  ('rate_limits',               '{"orders_per_s": 5, "cancels_per_s": 10, "market_data_per_s": 10, "account_per_s": 2}'),
  ('max_exposure_cents',        '{"live": 0, "paper": 0}'),
  ('scores_url',                '"https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"');

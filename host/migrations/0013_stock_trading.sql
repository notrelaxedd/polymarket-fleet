-- Stocks on Alpaca, step 9.2 to 9.5 (docs/ALPACA.md "Step 9"): stock models and their
-- lineages, assignments with their own cash, orders, fills, positions, daily marks
-- and the broker's state. Money in bigint cents, quantities in whole shares.

CREATE TABLE stock_models (
  id                  bigserial PRIMARY KEY,
  lineage_id          bigint,                          -- the first model of the lineage (its own id when NULL)
  family              text NOT NULL,
  params              jsonb NOT NULL,
  params_hash         text NOT NULL,
  summary             text,
  status              text NOT NULL DEFAULT 'candidate'
                      CHECK (status IN ('candidate', 'paper_ok', 'live_eligible', 'retired')),
  backtest_metrics    jsonb,                           -- the search-era walk-forward result
  validation_metrics  jsonb,                           -- the held-out years, NULL until validated
  created_by_job_id   uuid,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (family, params_hash)
);

CREATE TABLE stock_broker_state (
  id                  integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  environment         text CHECK (environment IN ('paper', 'live')),   -- from ALPACA_BASE_URL
  keys_present        boolean NOT NULL DEFAULT false,
  account_status      text,
  equity_cents        bigint,
  cash_cents          bigint,
  buying_power_cents  bigint,
  pattern_day_trader  boolean,
  daytrade_count      integer,
  market_open         boolean,
  session_date        date,                            -- the trading day the clock refers to
  next_open           timestamptz,
  next_close          timestamptz,
  checked_at          timestamptz,
  last_error          text,
  warnings            jsonb NOT NULL DEFAULT '[]'      -- reconciliation notes shown on /stocks
);
INSERT INTO stock_broker_state (id) VALUES (1);

CREATE TABLE stock_assignments (
  id                  bigserial PRIMARY KEY,
  model_id            bigint NOT NULL REFERENCES stock_models(id),
  mode                text NOT NULL CHECK (mode IN ('paper', 'live')),
  symbols             text[] NOT NULL,
  bankroll_cents      bigint NOT NULL CHECK (bankroll_cents > 0),
  cash_cents          bigint NOT NULL,                 -- available, not reserved
  reserved_cents      bigint NOT NULL DEFAULT 0 CHECK (reserved_cents >= 0),
  realized_cents      bigint NOT NULL DEFAULT 0,
  status              text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'halted', 'closed')),
  halt_reason         text,
  job_id              uuid,                            -- its stock_trade job
  last_decision_date  date,                            -- the session it last proposed for
  created_by          text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX stock_assignments_one_live_per_model
  ON stock_assignments (model_id) WHERE mode = 'live' AND status IN ('active', 'halted');

CREATE TABLE stock_orders (
  id                  uuid PRIMARY KEY,                -- also the Alpaca client_order_id
  assignment_id       bigint NOT NULL REFERENCES stock_assignments(id),
  mode                text NOT NULL CHECK (mode IN ('paper', 'live')),
  session_date        date NOT NULL,
  client_request_id   text NOT NULL,
  symbol              text NOT NULL,
  side                text NOT NULL CHECK (side IN ('buy', 'sell')),
  qty                 integer NOT NULL CHECK (qty > 0),
  ref_price_cents     bigint NOT NULL CHECK (ref_price_cents > 0),
  reserved_cents      bigint NOT NULL DEFAULT 0 CHECK (reserved_cents >= 0),
  status              text NOT NULL CHECK (status IN (
                        'rejected', 'approved', 'submitting', 'open', 'partial', 'filled',
                        'cancel_requested', 'cancelled', 'expired', 'rejected_by_exchange')),
  reason              text,                            -- the reject code, or why it was cancelled
  rationale           text,
  exchange_order_id   text,
  filled_qty          integer NOT NULL DEFAULT 0 CHECK (filled_qty >= 0),
  avg_fill_price      double precision,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (assignment_id, client_request_id)
);
CREATE INDEX stock_orders_active ON stock_orders (status) WHERE status IN ('approved', 'submitting', 'open', 'partial', 'cancel_requested');

CREATE TABLE stock_order_events (
  id          bigserial PRIMARY KEY,
  order_id    uuid NOT NULL REFERENCES stock_orders(id),
  ts          timestamptz NOT NULL DEFAULT now(),
  from_status text,
  to_status   text NOT NULL,
  actor       text NOT NULL,
  detail      jsonb
);

CREATE TABLE stock_fills (
  id                bigserial PRIMARY KEY,
  order_id          uuid NOT NULL REFERENCES stock_orders(id),
  exchange_fill_id  text NOT NULL UNIQUE,
  qty               integer NOT NULL CHECK (qty > 0),
  price             double precision NOT NULL CHECK (price > 0),
  cost_cents        bigint NOT NULL,                   -- qty * price in cents, rounded half up
  ts                timestamptz NOT NULL
);

CREATE TABLE stock_positions (
  assignment_id   bigint NOT NULL REFERENCES stock_assignments(id),
  symbol          text NOT NULL,
  qty             integer NOT NULL CHECK (qty >= 0),   -- long only
  cost_cents      bigint NOT NULL,                     -- total cost basis of qty
  updated_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (assignment_id, symbol)
);

CREATE TABLE stock_marks (
  assignment_id    bigint NOT NULL REFERENCES stock_assignments(id),
  session_date     date NOT NULL,
  equity_cents     bigint NOT NULL,                    -- cash + reserved + positions at the close
  positions_cents  bigint NOT NULL,
  PRIMARY KEY (assignment_id, session_date)
);

INSERT INTO settings (key, value) VALUES
  ('stock_decision_lead_min',      '20'),
  ('stock_trade_tick_s',           '30'),
  ('stock_cost_bps',               '5'),
  ('stock_price_band',             '0.05'),
  ('stock_max_order_cents',        '100000'),
  ('stock_max_position_cents',     '250000'),
  ('stock_default_bankroll_cents', '1000000'),
  ('stock_max_daily_loss_cents',   '{"paper": 100000, "live": 20000}'),
  ('stock_max_assignments',        '3'),
  ('stock_backtest_years',         '[2017, 2023]'),
  ('stock_validation_years',       '[2024, null]'),
  ('thresholds_stock_backtest',    '{"min_sharpe": 0.5, "max_drawdown": 0.30, "min_trades": 30, "min_validation_sharpe": 0.0}'),
  ('thresholds_stock_paper',       '{"min_days": 20, "min_return": -0.02, "max_drawdown": 0.15}'),
  ('stock_broker_poll_s',          '30'),
  ('stock_orders_poll_s',          '5')
ON CONFLICT (key) DO NOTHING;

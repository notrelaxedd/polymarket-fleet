-- Stocks on Alpaca, step 9.1 (docs/ALPACA.md "Step 9"): the instruments the owner
-- follows and their daily bars, fetched by the exchange process from Alpaca's market
-- data API. Read-only data: nothing here places or sizes an order.

CREATE TABLE instruments (
  symbol          text PRIMARY KEY CHECK (symbol ~ '^[A-Z][A-Z0-9.]{0,9}$'),
  asset_class     text NOT NULL DEFAULT 'us_equity',
  name            text,
  exchange        text,
  status          text,
  tradable        boolean,
  fractionable    boolean,
  shortable       boolean,
  bars_through    timestamptz,          -- the newest daily bar stored
  bars_count      integer NOT NULL DEFAULT 0,
  fetched_at      timestamptz,          -- the last successful fetch of this symbol
  last_error      text,
  updated_at      timestamptz NOT NULL DEFAULT now()
);

-- One row per symbol, timeframe and bar start. Prices are split- and dividend-adjusted
-- (Alpaca adjustment=all) and the whole history is fetched again once a day, so an old
-- bar changes when a split or a dividend lands and the series stays consistent.
CREATE TABLE stock_bars (
  symbol       text NOT NULL REFERENCES instruments(symbol) ON DELETE CASCADE,
  timeframe    text NOT NULL CHECK (timeframe IN ('1Day')),
  ts           timestamptz NOT NULL,
  open         double precision NOT NULL,
  high         double precision NOT NULL,
  low          double precision NOT NULL,
  close        double precision NOT NULL,
  volume       double precision NOT NULL,
  trade_count  integer,
  vwap         double precision,
  feed         text NOT NULL,
  PRIMARY KEY (symbol, timeframe, ts)
);

INSERT INTO settings (key, value) VALUES
  ('stocks_enabled',       'true'),
  ('stock_symbols',        '["SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM", "XOM", "UNH", "V", "COST", "HD", "KO", "PG", "WMT"]'),
  ('stock_history_start',  '"2016-01-01"'),
  ('stock_history_feed',   '"sip"'),
  ('stock_bars_hour',      '18')
ON CONFLICT (key) DO NOTHING;

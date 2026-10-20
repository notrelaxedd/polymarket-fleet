import type Database from "better-sqlite3";

/**
 * Ordered schema migrations. Index i upgrades user_version i -> i+1.
 * Never edit a shipped migration; append a new one.
 */
export const MIGRATIONS: string[] = [
  // v1: market data recording (step 1) + tables for trading/drawings/alerts (later steps)
  `
  CREATE TABLE IF NOT EXISTS bars (
    t INTEGER NOT NULL,           -- bar open time, ms UTC
    d INTEGER NOT NULL,           -- duration, seconds (1 live, 60 seeded)
    o REAL NOT NULL,
    h REAL NOT NULL,
    l REAL NOT NULL,
    c REAL NOT NULL,
    PRIMARY KEY (t, d)
  ) WITHOUT ROWID;
  CREATE INDEX IF NOT EXISTS bars_d_t ON bars (d, t);

  CREATE TABLE IF NOT EXISTS ticks (
    ts INTEGER NOT NULL,
    price REAL NOT NULL,
    coinbase REAL,
    kraken REAL,
    bitstamp REAL
  );
  CREATE INDEX IF NOT EXISTS ticks_ts ON ticks (ts);

  CREATE TABLE IF NOT EXISTS odds (
    ts INTEGER NOT NULL,
    window_id TEXT NOT NULL,
    up_bid REAL,
    up_ask REAL,
    down_bid REAL,
    down_ask REAL,
    up_mid REAL
  );
  CREATE INDEX IF NOT EXISTS odds_ts ON odds (ts);
  CREATE INDEX IF NOT EXISTS odds_window_ts ON odds (window_id, ts);

  CREATE TABLE IF NOT EXISTS fair (
    ts INTEGER NOT NULL,
    window_id TEXT NOT NULL,
    fair REAL NOT NULL,
    sigma REAL NOT NULL,
    t_left REAL NOT NULL,
    edge REAL
  );
  CREATE INDEX IF NOT EXISTS fair_ts ON fair (ts);
  CREATE INDEX IF NOT EXISTS fair_window_ts ON fair (window_id, ts);

  CREATE TABLE IF NOT EXISTS windows (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    ptb REAL,
    up_instrument TEXT NOT NULL,
    down_instrument TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    settle_price REAL,
    updated_ts INTEGER NOT NULL
  );
  CREATE INDEX IF NOT EXISTS windows_start ON windows (start_ts);
  CREATE INDEX IF NOT EXISTS windows_end ON windows (end_ts);

  CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL           -- JSON
  );

  -- ---- step 3+: trading ----
  CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    client_order_id TEXT,
    external_id TEXT,
    mode TEXT NOT NULL,           -- paper | live
    window_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    side TEXT NOT NULL,           -- up | down
    action TEXT NOT NULL,         -- buy | sell
    type TEXT NOT NULL,           -- limit | marketable
    limit_price REAL,             -- cents
    max_slippage REAL,            -- cents
    qty REAL NOT NULL,            -- contracts
    notional_usd REAL,
    filled_qty REAL NOT NULL DEFAULT 0,
    avg_fill REAL,                -- cents
    fees REAL NOT NULL DEFAULT 0, -- USD
    status TEXT NOT NULL,         -- pending | open | partial | filled | cancelled | rejected
    reason TEXT,                  -- manual | tp | sl | btc_stop | time_exit | kill
    bracket_id TEXT,
    error TEXT,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL
  );
  CREATE INDEX IF NOT EXISTS orders_created ON orders (created_ts);
  CREATE INDEX IF NOT EXISTS orders_window ON orders (window_id);

  CREATE TABLE IF NOT EXISTS fills (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    external_id TEXT,
    mode TEXT NOT NULL,
    window_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    side TEXT NOT NULL,
    action TEXT NOT NULL,
    price REAL NOT NULL,          -- cents
    qty REAL NOT NULL,
    fee REAL NOT NULL DEFAULT 0,  -- USD
    slippage REAL,                -- cents vs limit (positive = worse)
    ts INTEGER NOT NULL
  );
  CREATE INDEX IF NOT EXISTS fills_ts ON fills (ts);
  CREATE INDEX IF NOT EXISTS fills_order ON fills (order_id);

  CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    window_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    avg_price REAL NOT NULL,      -- cents
    realized_pnl REAL NOT NULL DEFAULT 0, -- USD
    fees REAL NOT NULL DEFAULT 0, -- USD
    status TEXT NOT NULL,         -- open | closed | settled
    opened_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    closed_ts INTEGER
  );
  CREATE INDEX IF NOT EXISTS positions_status ON positions (status);
  CREATE INDEX IF NOT EXISTS positions_window ON positions (window_id);
  CREATE INDEX IF NOT EXISTS positions_opened ON positions (opened_ts);

  CREATE TABLE IF NOT EXISTS brackets (
    id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL,
    window_id TEXT NOT NULL,
    tp REAL,                      -- cents
    sl REAL,                      -- cents
    btc_stop REAL,                -- USD
    time_exit_sec REAL,           -- exit at T- seconds
    max_slippage REAL,            -- cents
    status TEXT NOT NULL,         -- active | triggered | cancelled | done
    trigger_reason TEXT,
    triggered_ts INTEGER,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL
  );
  CREATE INDEX IF NOT EXISTS brackets_position ON brackets (position_id);
  CREATE INDEX IF NOT EXISTS brackets_status ON brackets (status);

  -- ---- step 4: drawings ----
  CREATE TABLE IF NOT EXISTS drawings (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,           -- trend | ray | hline | vline | rect | fib | text | measure | sticker
    data TEXT NOT NULL,           -- JSON (points, style, text, sticker id...)
    locked INTEGER NOT NULL DEFAULT 0,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL
  );
  CREATE INDEX IF NOT EXISTS drawings_updated ON drawings (updated_ts);

  -- ---- step 6: alerts ----
  CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,           -- btc | up_odds | down_odds | edge | ptb_cross
    condition TEXT NOT NULL,      -- JSON { op, value, ... }
    enabled INTEGER NOT NULL DEFAULT 1,
    once INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    last_fired_ts INTEGER,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL
  );

  CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    window_id TEXT,
    value REAL,
    message TEXT NOT NULL
  );
  CREATE INDEX IF NOT EXISTS alert_events_ts ON alert_events (ts);
  CREATE INDEX IF NOT EXISTS alert_events_alert ON alert_events (alert_id, ts);
  `,
];

export const SCHEMA_VERSION = MIGRATIONS.length;

/** Apply pending migrations, each in its own transaction. Returns the resulting user_version. */
export function migrate(db: Database.Database): number {
  const current = db.pragma("user_version", { simple: true }) as number;
  if (current > MIGRATIONS.length) {
    throw new Error(`Database schema v${current} is newer than this build (v${MIGRATIONS.length})`);
  }
  for (let v = current; v < MIGRATIONS.length; v++) {
    const sql = MIGRATIONS[v]!;
    db.transaction(() => {
      db.exec(sql);
      db.pragma(`user_version = ${v + 1}`);
    })();
  }
  return db.pragma("user_version", { simple: true }) as number;
}

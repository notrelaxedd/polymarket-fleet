-- Step 5: live trading state and settings. See docs/LIVE.md.
ALTER TABLE exchange_state
  ADD COLUMN auth_failures           integer NOT NULL DEFAULT 0,
  ADD COLUMN credentials_present     boolean NOT NULL DEFAULT false,
  ADD COLUMN last_auth_error         text,
  ADD COLUMN open_orders_checked_at  timestamptz,
  ADD COLUMN live_enabled_at         timestamptz,
  ADD COLUMN live_enabled_by         text;

INSERT INTO settings (key, value) VALUES
  ('auth_probe_interval_s',  '300'),
  ('buying_power_max_age_s', '300'),
  ('submitting_grace_s',     '60'),
  ('auto_kill',              '{"auth_failures": 3, "clock_skew_ms": 30000}'),
  ('smoke_hold_seconds',     '10'),
  ('live_fills_poll_s',      '2'),
  ('open_orders_audit_s',    '60');

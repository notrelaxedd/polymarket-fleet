-- Fleet UI (docs/FLEET_UI_CONTRACT.md, docs/PROTOCOL.md "Fleet UI additions"): the
-- machine health a worker reports on each heartbeat, whether its install can reboot
-- itself, and the owner's pending reboot request. Heartbeats move to every 3 s and a
-- worker counts as offline after 30 s without one; a value the owner changed stays.

ALTER TABLE workers
  ADD COLUMN temp_c              real,
  ADD COLUMN boot_media          text CHECK (boot_media IN ('flash', 'ssd', 'hdd', 'unknown')),
  ADD COLUMN wear_pct            real,
  ADD COLUMN disk_gb_written     real,
  ADD COLUMN can_reboot          boolean NOT NULL DEFAULT false,
  ADD COLUMN reboot_id           text,
  ADD COLUMN reboot_requested_at timestamptz;

UPDATE settings SET value = '3', updated_at = now()
 WHERE key = 'heartbeat_seconds' AND value = '5'::jsonb;
UPDATE settings SET value = '30', updated_at = now()
 WHERE key = 'online_after_seconds' AND value = '15'::jsonb;

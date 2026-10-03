-- Step 1 review fixes.
-- prev_token_hash: register accepts the previous token once more so a lost register
-- response cannot brick a worker; the first heartbeat with the new token clears it.
ALTER TABLE workers ADD COLUMN prev_token_hash text;

-- target_auto: the target was chosen by the system (any_idle pick or the dispatcher),
-- so release and lease expiry clear it and the job can be re-assigned elsewhere.
ALTER TABLE jobs ADD COLUMN target_auto boolean NOT NULL DEFAULT false;

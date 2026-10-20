import { z } from "zod";
import type { Settings } from "../../../shared/protocol.ts";
import type { TerminalDb } from "./index.ts";

/** Persisted (user-editable) settings. feeNote is derived, never stored. */
export type StoredSettings = Omit<Settings, "feeNote">;

export const SettingsPatchSchema = z
  .object({
    mode: z.enum(["paper", "live"]),
    volLookbackMin: z.number().finite().min(1).max(24 * 60),
    feeCentsPerContract: z.number().finite().min(0).max(100).nullable(),
    // accepted (clients may echo the full Settings object) but ignored
    feeNote: z.string().optional(),
  })
  .partial()
  .strict();

export type SettingsPatch = z.infer<typeof SettingsPatchSchema>;

const KEYS = {
  mode: "mode",
  volLookbackMin: "volLookbackMin",
  feeCentsPerContract: "feeCentsPerContract",
} as const;

/**
 * Load settings: a value stored in the DB (only ever written by an explicit user update via
 * saveSettings) wins; any key that is missing or invalid uses `defaults` (env). Defaults are
 * NOT persisted, so a later .env change still applies to keys the user never set.
 */
export function loadSettings(db: TerminalDb, defaults: StoredSettings): StoredSettings {
  const stored = db.allSettings();
  const out: StoredSettings = { ...defaults };
  for (const key of Object.keys(KEYS) as (keyof StoredSettings)[]) {
    if (!(key in stored)) continue;
    const parsed = SettingsPatchSchema.safeParse({ [key]: stored[key] });
    const value = parsed.success ? parsed.data[key] : undefined;
    if (value !== undefined) (out as Record<string, unknown>)[key] = value;
  }
  return out;
}

export function saveSettings(db: TerminalDb, s: Partial<StoredSettings>): void {
  db.raw.transaction(() => {
    if (s.mode !== undefined) db.setSetting(KEYS.mode, s.mode);
    if (s.volLookbackMin !== undefined) db.setSetting(KEYS.volLookbackMin, s.volLookbackMin);
    if (s.feeCentsPerContract !== undefined) db.setSetting(KEYS.feeCentsPerContract, s.feeCentsPerContract);
  })();
}

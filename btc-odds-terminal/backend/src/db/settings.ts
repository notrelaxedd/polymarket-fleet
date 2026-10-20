import { z } from "zod";
import type { RiskLimits, Settings } from "../../../shared/protocol.ts";
import { DEFAULT_RISK_LIMITS } from "../../../shared/protocol.ts";
import type { TerminalDb } from "./index.ts";

/** Persisted (user-editable) settings. feeNote / liveAvailable are derived, never stored; risk limits are
 *  stored under their own key (loadRiskLimits / saveSettings({ risk })). */
export type StoredSettings = Omit<Settings, "feeNote" | "liveAvailable" | "risk">;

const usd = z.number().finite().min(0).max(1_000_000);

export const RiskLimitsPatchSchema = z
  .object({
    // 0 would disable trading at the first cent of loss (and a cleared form field must not send it)
    dailyLossLimitUsd: usd.refine((v) => v > 0, "must be greater than 0"),
    maxExposureUsd: usd,
    maxLossPerWindowUsd: usd,
    maxPositionUsd: usd,
  })
  .partial()
  .strict();

export const SettingsPatchSchema = z
  .object({
    mode: z.enum(["paper", "live"]),
    volLookbackMin: z.number().finite().min(1).max(24 * 60),
    feeCentsPerContract: z.number().finite().min(0).max(100).nullable(),
    /** merged over the current limits */
    risk: RiskLimitsPatchSchema,
    // accepted (clients may echo the full Settings object) but ignored
    feeNote: z.string().optional(),
    liveAvailable: z.boolean().optional(),
  })
  .partial()
  .strict();

export type SettingsPatch = z.infer<typeof SettingsPatchSchema>;

const KEYS = {
  mode: "mode",
  volLookbackMin: "volLookbackMin",
  feeCentsPerContract: "feeCentsPerContract",
} as const;
const RISK_KEY = "risk";

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

/** Stored risk limits merged field by field over `defaults` (DEFAULT_RISK_LIMITS). */
export function loadRiskLimits(db: TerminalDb, defaults: RiskLimits = DEFAULT_RISK_LIMITS): RiskLimits {
  const parsed = RiskLimitsPatchSchema.safeParse(db.getSetting(RISK_KEY) ?? {});
  return { ...defaults, ...(parsed.success ? parsed.data : {}) };
}

/** Persist explicitly changed keys. `risk` must be the full (merged) limits object. */
export function saveSettings(db: TerminalDb, s: Partial<StoredSettings> & { risk?: RiskLimits }): void {
  db.raw.transaction(() => {
    if (s.mode !== undefined) db.setSetting(KEYS.mode, s.mode);
    if (s.volLookbackMin !== undefined) db.setSetting(KEYS.volLookbackMin, s.volLookbackMin);
    if (s.feeCentsPerContract !== undefined) db.setSetting(KEYS.feeCentsPerContract, s.feeCentsPerContract);
    if (s.risk !== undefined) db.setSetting(RISK_KEY, s.risk);
  })();
}

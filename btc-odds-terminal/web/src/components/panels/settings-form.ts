// Settings form model + validation (pure; used by header/SettingsMenu.tsx). OWNER: trading-ui.
import type { RiskLimits, RiskPatch, RiskState, Settings, SettingsPatch, TradingMode } from "@shared/protocol";
import { DEFAULT_RISK_LIMITS } from "@shared/protocol";
import { usd } from "@/lib/format";
import { parseNum } from "./ticket-math";

export const RISK_FIELDS: { key: keyof RiskLimits; label: string }[] = [
  { key: "dailyLossLimitUsd", label: "Daily loss limit" },
  { key: "maxExposureUsd", label: "Max open exposure" },
  { key: "maxLossPerWindowUsd", label: "Max loss per window" },
  { key: "maxPositionUsd", label: "Max position" },
];

export type Form = { mode: TradingMode; vol: string; fee: string } & Record<keyof RiskLimits, string>;
export type FormErrors = Partial<Record<keyof Form, string>>;

const MAX_LIMIT_USD = 1_000_000;

export function toForm(s: Settings | null): Form {
  const r = s?.risk ?? DEFAULT_RISK_LIMITS;
  return {
    mode: s?.mode ?? "paper",
    vol: String(s?.volLookbackMin ?? 30),
    fee: s?.feeCentsPerContract == null ? "" : String(s.feeCentsPerContract),
    dailyLossLimitUsd: String(r.dailyLossLimitUsd),
    maxExposureUsd: String(r.maxExposureUsd),
    maxLossPerWindowUsd: String(r.maxLossPerWindowUsd),
    maxPositionUsd: String(r.maxPositionUsd),
  };
}

/** Field errors of the settings form (exported for tests). */
export function validateSettingsForm(f: Form, liveAvailable: boolean): FormErrors {
  const e: FormErrors = {};
  const vol = parseNum(f.vol);
  if (vol == null || !Number.isInteger(vol) || vol < 5 || vol > 240) e.vol = "Whole minutes, 5–240.";
  if (f.fee.trim() !== "") {
    const fee = parseNum(f.fee);
    if (fee == null || fee < 0 || fee > 50) e.fee = "0–50¢ per contract, or blank for [FEE].";
  }
  for (const { key } of RISK_FIELDS) {
    const v = parseNum(f[key]);
    if (v == null || v <= 0 || v > MAX_LIMIT_USD) e[key] = `Above $0, at most ${usd(MAX_LIMIT_USD, 0)}.`;
  }
  if (f.mode === "live" && !liveAvailable) e.mode = "No Polymarket US keys in the backend .env.";
  return e;
}

/**
 * PUT /api/settings body: only the keys the user changed. The backend persists every key present as an explicit
 * override, so an unchanged mode / vol / fee must not be sent (a later .env change would stop applying, and a
 * blank fee would be stored as an explicit null = [FEE]).
 */
export function settingsPatchFromForm(f: Form, s: Settings): SettingsPatch {
  const p: SettingsPatch = {};
  if (f.mode !== s.mode) p.mode = f.mode;
  const vol = parseNum(f.vol);
  if (vol != null && vol !== s.volLookbackMin) p.volLookbackMin = vol;
  const fee = f.fee.trim() === "" ? null : parseNum(f.fee);
  if (fee !== (s.feeCentsPerContract ?? null)) p.feeCentsPerContract = fee;
  return p;
}

/** PUT /api/risk body: the limits that differ from the stored ones. */
export function riskPatchFromForm(f: Form, s: Settings): RiskPatch {
  const cur = s.risk ?? DEFAULT_RISK_LIMITS;
  const p: RiskPatch = {};
  for (const { key } of RISK_FIELDS) {
    const v = parseNum(f[key]);
    if (v != null && v !== cur[key]) p[key] = v;
  }
  return p;
}

export type SavePart = "settings" | "risk";
export interface SaveOutcome {
  applied: SavePart[];
  /** the part that failed (the parts after it were not attempted) */
  failed: SavePart | null;
  error: unknown;
}

/**
 * Save the form as up to two calls, ordered so a failure never leaves trading MORE permissive than intended:
 * switching to live (or staying) applies the risk limits first and the mode last; switching to paper applies the
 * mode first. Unchanged parts are skipped (no call at all when nothing changed).
 */
export async function saveSettingsForm(
  f: Form,
  s: Settings,
  io: {
    putSettings: (p: SettingsPatch) => Promise<Settings>;
    putRisk: (p: RiskPatch) => Promise<RiskState>;
    onSettings: (next: Settings) => void;
    onRisk: (next: RiskState) => void;
  },
): Promise<SaveOutcome> {
  const sp = settingsPatchFromForm(f, s);
  const rp = riskPatchFromForm(f, s);
  const steps: { part: SavePart; run: () => Promise<void> }[] = [];
  const settingsStep = { part: "settings" as const, run: async () => io.onSettings(await io.putSettings(sp)) };
  const riskStep = { part: "risk" as const, run: async () => io.onRisk(await io.putRisk(rp)) };
  const hasSettings = Object.keys(sp).length > 0;
  const hasRisk = Object.keys(rp).length > 0;
  if (sp.mode === "paper") {
    if (hasSettings) steps.push(settingsStep);
    if (hasRisk) steps.push(riskStep);
  } else {
    if (hasRisk) steps.push(riskStep);
    if (hasSettings) steps.push(settingsStep);
  }
  const applied: SavePart[] = [];
  for (const st of steps) {
    try {
      await st.run();
      applied.push(st.part);
    } catch (error) {
      return { applied, failed: st.part, error };
    }
  }
  return { applied, failed: null, error: null };
}

const PART_LABEL: Record<SavePart, string> = { settings: "Mode, vol and fee", risk: "Risk limits" };

/** Error line for a failed save: names what was applied and what was not. */
export function saveFailureText(o: SaveOutcome, reason: string): string {
  if (!o.failed) return "";
  const notSaved = `${PART_LABEL[o.failed]} NOT saved: ${reason}`;
  return o.applied.length ? `${o.applied.map((p) => PART_LABEL[p]).join(" and ")} saved. ${notSaved}` : notSaved;
}

// Formatting helpers. Times are stored UTC (epoch ms) and displayed in America/New_York.
const TZ = "America/New_York";

const fmtHM = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "numeric", minute: "2-digit", hour12: true });
const fmtHMS = new Intl.DateTimeFormat("en-US", {
  timeZone: TZ,
  hour: "numeric",
  minute: "2-digit",
  second: "2-digit",
  hour12: true,
});
const fmtParts = new Intl.DateTimeFormat("en-US", {
  timeZone: TZ,
  hour: "numeric",
  minute: "2-digit",
  hour12: true,
});

/** "10:30 AM" */
export function etTime(ms: number): string {
  return fmtHM.format(ms);
}

/** "10:30:05 AM" */
export function etTimeSec(ms: number): string {
  return fmtHMS.format(ms);
}

function parts(ms: number) {
  const p = fmtParts.formatToParts(ms);
  const get = (t: string) => p.find((x) => x.type === t)?.value ?? "";
  return { h: get("hour"), m: get("minute"), ap: get("dayPeriod") };
}

/** "10:30–10:45 AM ET" (period shown once when both ends share it) */
export function etRange(startMs: number, endMs: number): string {
  const a = parts(startMs);
  const b = parts(endMs);
  const left = a.ap === b.ap ? `${a.h}:${a.m}` : `${a.h}:${a.m} ${a.ap}`;
  return `${left}–${b.h}:${b.m} ${b.ap} ET`;
}

/** "10:30–10:45" without period/zone (for compact chips) */
export function etRangeShort(startMs: number, endMs: number): string {
  const a = parts(startMs);
  const b = parts(endMs);
  return `${a.h}:${a.m}–${b.h}:${b.m}`;
}

/** mm:ss from a non-negative duration in ms (floors at 0). */
export function mmss(ms: number): string {
  const s = Math.max(0, Math.ceil(ms / 1000));
  const m = Math.floor(s / 60);
  return `${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

/** "6:45 left" style, seconds -> m:ss (no zero pad on minutes) */
export function mSS(sec: number): string {
  const s = Math.max(0, Math.round(sec));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export const NA = "—";

/** Cents: 26 -> "26¢", 26.4 -> "26.4¢" (integer when close). */
export function cents(v: number | null | undefined, digits = 0): string {
  if (v == null || !Number.isFinite(v)) return NA;
  return `${v.toFixed(digits)}¢`;
}

/** Cents without symbol */
export function centsNum(v: number | null | undefined, digits = 0): string {
  if (v == null || !Number.isFinite(v)) return NA;
  return v.toFixed(digits);
}

const usd2 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
const usd0 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 0, maximumFractionDigits: 0 });

export function usd(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return NA;
  return (digits === 0 ? usd0 : usd2).format(v);
}

/** "+$12.34" / "−$5.00" */
export function signedUsd(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return NA;
  const s = v < 0 ? "−" : "+";
  return `${s}${usd(Math.abs(v), digits)}`;
}

/** "+3.2" / "−1.5" */
export function signed(v: number | null | undefined, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return NA;
  const s = v < 0 ? "−" : "+";
  return `${s}${Math.abs(v).toFixed(digits)}`;
}

/** Signed cents: "+2.1¢" */
export function signedCents(v: number | null | undefined, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return NA;
  return `${signed(v, digits)}¢`;
}

/** BTC price, "67,123.45" */
export function btc(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return NA;
  return v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** Compact age: 4s, 2m */
export function ageLabel(ageMs: number | null | undefined): string {
  if (ageMs == null) return NA;
  const s = Math.round(ageMs / 1000);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m`;
}

export function feedLabel(id: string): string {
  switch (id) {
    case "coinbase":
      return "Coinbase";
    case "kraken":
      return "Kraken";
    case "bitstamp":
      return "Bitstamp";
    case "polymarket":
      return "Polymarket";
    default:
      return id;
  }
}

export function feedShort(id: string): string {
  switch (id) {
    case "coinbase":
      return "CB";
    case "kraken":
      return "KR";
    case "bitstamp":
      return "BS";
    case "polymarket":
      return "PM";
    default:
      return id;
  }
}

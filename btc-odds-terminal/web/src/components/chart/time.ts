// America/New_York display helpers for the chart (axis tick marks, crosshair label, band labels).
import { TickMarkType, type Time, type UTCTimestamp } from "lightweight-charts";

const TZ = "America/New_York";

const fHM = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "numeric", minute: "2-digit", hour12: true });
const fHMS = new Intl.DateTimeFormat("en-US", {
  timeZone: TZ,
  hour: "numeric",
  minute: "2-digit",
  second: "2-digit",
  hour12: true,
});
const fDay = new Intl.DateTimeFormat("en-US", { timeZone: TZ, month: "short", day: "numeric" });
const fCross = new Intl.DateTimeFormat("en-US", {
  timeZone: TZ,
  weekday: "short",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
  second: "2-digit",
  hour12: true,
});

// Intl formatting is comparatively slow and the axis asks for the same labels repeatedly.
const cache = new Map<string, string>();
function cached(key: string, f: () => string): string {
  let v = cache.get(key);
  if (v === undefined) {
    if (cache.size > 5000) cache.clear();
    v = f();
    cache.set(key, v);
  }
  return v;
}

/** "10:30" (12h clock, no period) */
export function hm(ms: number): string {
  return cached(`hm${ms}`, () => fHM.format(ms).replace(/\s?[AP]M$/, ""));
}
/** "10:30 AM" */
export function hmAp(ms: number): string {
  return cached(`ha${ms}`, () => fHM.format(ms));
}
/** "10:30:15" */
export function hms(ms: number): string {
  return cached(`hs${ms}`, () => fHMS.format(ms).replace(/\s?[AP]M$/, ""));
}

/** "10:15–10:30" */
export function rangeLabel(startMs: number, endMs: number): string {
  return `${hm(startMs)}–${hm(endMs)}`;
}

/** seconds -> "6:45" */
export function mss(sec: number): string {
  const s = Math.max(0, Math.floor(sec));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export const toSec = (ms: number) => Math.floor(ms / 1000) as UTCTimestamp;
export const timeToMs = (t: Time) => (t as number) * 1000;

export function tickMarkFormatter(time: Time, type: TickMarkType): string {
  const ms = timeToMs(time);
  switch (type) {
    case TickMarkType.Year:
    case TickMarkType.Month:
    case TickMarkType.DayOfMonth:
      return cached(`d${ms}`, () => fDay.format(ms));
    case TickMarkType.Time:
      return hm(ms);
    case TickMarkType.TimeWithSeconds:
    default:
      return hms(ms);
  }
}

export function crosshairTimeFormatter(time: Time): string {
  const ms = timeToMs(time);
  return cached(`c${ms}`, () => fCross.format(ms).replace(",", ""));
}

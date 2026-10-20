// Pure discovery logic: raw gateway events -> MarketWindow candidates, current/next selection,
// status, rollover. No I/O here (see tracker.ts), so it is unit-testable with a fixed clock.
//
// What is VERIFIED (official SDK types): events carry { id, slug, title, description?, startTime?,
// endTime?, active, closed, markets?: [{ id, slug, title, outcome, active, closed }], series?: { id, slug, title } };
// a market is a binary instrument traded via ORDER_INTENT_BUY_LONG / BUY_SHORT.
// What is UNVERIFIED (docs unreachable from the build sandbox): how the "BTC Up or Down: 15 min" series is
// titled/slugged, whether one market (long = Up, short = Down) or two markets (Up, Down) represent a window,
// and which field carries the price to beat. The parsers below accept every plausible shape and the
// heuristics are overridable through env (config.ts).

import type { MarketWindow, WindowResult, WindowStatus } from "../../../shared/protocol.ts";
import type { PolyConfig } from "./config.ts";
import { DEFAULT_PTB_FIELDS } from "./config.ts";
import { isObj, num, toMs } from "./normalize.ts";

export type Side = "long" | "short";

/** A window plus what the tracker needs to price and settle it. */
export interface DiscoveredWindow {
  window: MarketWindow;
  /** market slug whose `upSide` is the Up outcome */
  upSlug: string;
  upSide: Side;
  downSlug: string;
  downSide: Side;
  /** priceToBeat came from exchange metadata (true) or is null / a BRTI-proxy fallback (false) */
  ptbFromMetadata: boolean;
}

export type DiscoveryConfig = Pick<PolyConfig, "slugRegex" | "ptbFields" | "windowMs">;

export const DEFAULT_DISCOVERY: DiscoveryConfig = {
  slugRegex: null,
  ptbFields: DEFAULT_PTB_FIELDS,
  windowMs: 15 * 60 * 1000,
};

const DURATION_TOLERANCE_MS = 60_000;

// ---------------------------------------------------------------- instruments

export function instrumentOf(slug: string, side: Side): string {
  return `${slug}:${side}`;
}

export function parseInstrument(inst: string): { slug: string; side: Side } {
  const i = inst.lastIndexOf(":");
  if (i > 0) {
    const side = inst.slice(i + 1);
    if (side === "long" || side === "short") return { slug: inst.slice(0, i), side };
  }
  return { slug: inst, side: "long" };
}

/** Long-side top of book (cents) -> the requested side's (bid, ask). Short bid = 100 - long ask. */
export function sidePrices(
  long: { bid: number | null; ask: number | null },
  side: Side,
): { bid: number | null; ask: number | null } {
  if (side === "long") return { bid: long.bid, ask: long.ask };
  const r = (v: number) => Math.round((100 - v) * 10) / 10;
  return { bid: long.ask === null ? null : r(long.ask), ask: long.bid === null ? null : r(long.bid) };
}

// ---------------------------------------------------------------- text heuristics

function s(v: unknown): string {
  return typeof v === "string" ? v : "";
}

function textOf(...objs: Array<Record<string, unknown> | null | undefined>): string {
  const parts: string[] = [];
  for (const o of objs) {
    if (!o) continue;
    parts.push(s(o.slug), s(o.title), s(o.ticker));
    if (isObj(o.series)) parts.push(s(o.series.slug), s(o.series.title));
  }
  return parts.join(" ");
}

const RE_BTC = /\b(btc|bitcoin)\b|btc-|bitcoin-/i;
const RE_UPDOWN = /up[\s_-]*(or|\/|&)?[\s_-]*down|updown/i;
const RE_15 = /(^|[^0-9])15[\s_-]*(m|min|mins|minute|minutes)\b|15m/i;

export function looksLikeBtcUpDown(text: string): boolean {
  return RE_BTC.test(text) && RE_UPDOWN.test(text);
}

// ---------------------------------------------------------------- times

const START_KEYS = ["startTime", "startDate", "openTime", "windowStart", "startTs"];
const END_KEYS = ["endTime", "endDate", "closeTime", "expirationTime", "expirationDate", "windowEnd", "endTs"];

function firstTime(o: Record<string, unknown> | null | undefined, keys: string[]): number | null {
  if (!o) return null;
  for (const k of keys) {
    const t = toMs(o[k]);
    if (t !== null && t > 1e12) return t;
  }
  return null;
}

/** 10-digit epoch-seconds token in a slug (e.g. "btc-updown-15m-1760970600"). UNVERIFIED pattern. */
export function slugEpochMs(slug: string): number | null {
  const m = /(?:^|[-_])(\d{10})(?:[-_]|$)/.exec(slug);
  return m ? Number(m[1]) * 1000 : null;
}

const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];

/** UTC ms for a wall-clock time in America/New_York. */
export function nyWallToUtc(y: number, mo: number, d: number, h: number, mi: number): number {
  const guess = Date.UTC(y, mo, d, h, mi);
  const fmt = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
  const offsetAt = (t: number) => {
    const p = Object.fromEntries(fmt.formatToParts(new Date(t)).map((x) => [x.type, x.value]));
    const asUtc = Date.UTC(Number(p.year), Number(p.month) - 1, Number(p.day), Number(p.hour) % 24, Number(p.minute));
    return asUtc - t; // NY offset (negative)
  };
  let t = guess - offsetAt(guess);
  t = guess - offsetAt(t);
  return t;
}

/**
 * "Bitcoin Up or Down - October 20, 10:30AM-10:45AM ET" -> { startTs, endTs } (UNVERIFIED title format,
 * modelled on Polymarket's international titles). Year defaults to the reference clock's NY year.
 */
export function parseTitleWindow(title: string, refNow: number): { startTs: number; endTs: number } | null {
  const re =
    /([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(?:(\d{4}),?\s*)?(\d{1,2})(?::(\d{2}))?\s*([AP]M)?\s*[-–]\s*(\d{1,2})(?::(\d{2}))?\s*([AP]M)/i;
  const m = re.exec(title);
  if (!m) return null;
  const mo = MONTHS.indexOf(m[1]!.slice(0, 3).toLowerCase());
  if (mo < 0) return null;
  const day = Number(m[2]);
  const year = m[3] ? Number(m[3]) : new Date(refNow).getUTCFullYear();
  const to24 = (h: number, ap: string | undefined) => {
    const a = (ap ?? "").toUpperCase();
    if (a === "PM" && h < 12) return h + 12;
    if (a === "AM" && h === 12) return 0;
    return h;
  };
  const endAp = m[9]!;
  const sh = to24(Number(m[4]), m[6] ?? endAp);
  const eh = to24(Number(m[7]), endAp);
  const startTs = nyWallToUtc(year, mo, day, sh, Number(m[5] ?? 0));
  let endTs = nyWallToUtc(year, mo, day, eh, Number(m[8] ?? 0));
  if (endTs <= startTs) endTs += 24 * 3600_000; // 11:45PM-12:00AM
  return { startTs, endTs };
}

export interface WindowTimes {
  startTs: number;
  endTs: number;
  /** both bounds came from explicit fields and are ~windowMs apart */
  explicit15: boolean;
}

export function extractTimes(
  market: Record<string, unknown>,
  event: Record<string, unknown> | null,
  cfg: DiscoveryConfig,
  refNow: number,
): WindowTimes | null {
  const W = cfg.windowMs;
  let start = firstTime(market, START_KEYS) ?? firstTime(event, START_KEYS);
  let end = firstTime(market, END_KEYS) ?? firstTime(event, END_KEYS);
  if (start !== null && end !== null && Math.abs(end - start - W) <= DURATION_TOLERANCE_MS) {
    return { startTs: start, endTs: end, explicit15: true };
  }
  const epoch = slugEpochMs(s(market.slug)) ?? (event ? slugEpochMs(s(event.slug)) : null);
  const titled = parseTitleWindow(s(market.title), refNow) ?? (event ? parseTitleWindow(s(event.title), refNow) : null);
  if (titled && Math.abs(titled.endTs - titled.startTs - W) <= DURATION_TOLERANCE_MS) {
    return { ...titled, explicit15: false };
  }
  if (epoch !== null) {
    // Polymarket's international up/down slugs carry the window START; accept an end-epoch too.
    if (end !== null && Math.abs(end - epoch) <= DURATION_TOLERANCE_MS) return { startTs: end - W, endTs: end, explicit15: false };
    return { startTs: epoch, endTs: epoch + W, explicit15: false };
  }
  if (end !== null) return { startTs: end - W, endTs: end, explicit15: false };
  if (start !== null) return { startTs: start, endTs: start + W, explicit15: false };
  return null;
}

// ---------------------------------------------------------------- price to beat

const PTB_TEXT_RES = [
  /price\s*to\s*beat[^0-9$]{0,20}\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)/i,
  /(?:strike|target|reference|opening|open|start(?:ing)?)\s*price[^0-9$]{0,20}\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)/i,
  /(?:at or above|above|greater than or equal to|>=|≥)\s*\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)/i,
];

const MIN_BTC_PRICE = 1000;

function ptbFromFields(o: Record<string, unknown> | null | undefined, fields: string[]): number | null {
  if (!o) return null;
  const scopes: Record<string, unknown>[] = [o];
  for (const k of ["metadata", "marketMetadata", "details", "settlementSource", "rules"]) {
    if (isObj(o[k])) scopes.push(o[k] as Record<string, unknown>);
  }
  for (const scope of scopes) {
    for (const f of fields) {
      const v = num(scope[f]);
      if (v !== null && v >= MIN_BTC_PRICE) return v;
    }
  }
  return null;
}

export function ptbFromText(text: string): number | null {
  for (const re of PTB_TEXT_RES) {
    const m = re.exec(text);
    if (m) {
      const v = Number(m[1]!.replace(/,/g, ""));
      if (Number.isFinite(v) && v >= MIN_BTC_PRICE) return v;
    }
  }
  return null;
}

/** Price to beat from metadata: configured fields (market, then event), then description/title text. */
export function extractPriceToBeat(
  market: Record<string, unknown>,
  event: Record<string, unknown> | null,
  fields: string[] = DEFAULT_PTB_FIELDS,
): number | null {
  return (
    ptbFromFields(market, fields) ??
    ptbFromFields(event, fields) ??
    ptbFromText([s(market.description), s(market.rules), s(market.title)].join("\n")) ??
    (event ? ptbFromText([s(event.description), s(event.rules), s(event.title)].join("\n")) : null)
  );
}

// ---------------------------------------------------------------- window status

export function statusAt(w: Pick<MarketWindow, "startTs" | "endTs" | "result">, now: number): WindowStatus {
  if (w.result) return "settled";
  if (now < w.startTs) return "upcoming";
  if (now < w.endTs) return "live";
  return "closed";
}

// ---------------------------------------------------------------- events -> windows

function outcomeSide(m: Record<string, unknown>): "up" | "down" | null {
  const o = `${s(m.outcome)} ${s(m.title)}`.toLowerCase();
  const up = /\bup\b|\bhigher\b|\babove\b/.test(o);
  const down = /\bdown\b|\blower\b|\bbelow\b/.test(o);
  if (up && !down) return "up";
  if (down && !up) return "down";
  const outcome = s(m.outcome).trim().toLowerCase();
  if (outcome === "up") return "up";
  if (outcome === "down") return "down";
  return null;
}

function marketsOf(ev: Record<string, unknown>): Record<string, unknown>[] {
  return Array.isArray(ev.markets) ? ev.markets.filter(isObj).filter((m) => s(m.slug) !== "") : [];
}

function build(
  idSlug: string,
  title: string,
  times: WindowTimes,
  up: { slug: string; side: Side },
  down: { slug: string; side: Side },
  ptb: number | null,
  now: number,
): DiscoveredWindow {
  const window: MarketWindow = {
    id: idSlug,
    title,
    startTs: times.startTs,
    endTs: times.endTs,
    priceToBeat: ptb,
    upInstrument: instrumentOf(up.slug, up.side),
    downInstrument: instrumentOf(down.slug, down.side),
    status: "upcoming",
    result: null,
    settlePrice: null,
  };
  window.status = statusAt(window, now);
  return {
    window,
    upSlug: up.slug,
    upSide: up.side,
    downSlug: down.slug,
    downSide: down.side,
    ptbFromMetadata: ptb !== null,
  };
}

/** True if the event (or one of its markets) is a 15-minute BTC Up/Down market. */
export function isBtc15mEvent(ev: Record<string, unknown>, cfg: DiscoveryConfig = DEFAULT_DISCOVERY, now = Date.now()): boolean {
  const mk = marketsOf(ev);
  if (cfg.slugRegex) {
    return cfg.slugRegex.test(s(ev.slug)) || mk.some((m) => cfg.slugRegex!.test(s(m.slug)));
  }
  const text = textOf(ev, ...mk);
  if (!looksLikeBtcUpDown(text)) return false;
  if (RE_15.test(text)) return true;
  // no "15m" in the text: accept only when explicit times say it is a 15-minute window
  const probe = mk[0] ?? ev;
  const t = extractTimes(probe, ev, cfg, now);
  return t?.explicit15 ?? false;
}

/**
 * Pure: raw gateway events -> windows. Handles both representations:
 *  - two markets in one event whose outcomes are Up and Down -> one window, each side = that market's long;
 *  - otherwise every market is its own window, Up = long (or short when its outcome reads "Down").
 */
export function windowsFromEvents(
  events: unknown[],
  now: number,
  cfg: DiscoveryConfig = DEFAULT_DISCOVERY,
): DiscoveredWindow[] {
  const out = new Map<string, DiscoveredWindow>();
  for (const evRaw of events) {
    if (!isObj(evRaw)) continue;
    const ev = evRaw;
    if (!isBtc15mEvent(ev, cfg, now)) continue;
    const mk = marketsOf(ev);
    const title = s(ev.title) || s(mk[0]?.title) || "BTC Up or Down · 15m";

    const ups = mk.filter((m) => outcomeSide(m) === "up");
    const downs = mk.filter((m) => outcomeSide(m) === "down");
    if (mk.length === 2 && ups.length === 1 && downs.length === 1) {
      const up = ups[0]!;
      const down = downs[0]!;
      const times = extractTimes(up, ev, cfg, now);
      if (!times) continue;
      const ptb = extractPriceToBeat(up, ev, cfg.ptbFields) ?? extractPriceToBeat(down, null, cfg.ptbFields);
      const dw = build(s(up.slug), title, times, { slug: s(up.slug), side: "long" }, { slug: s(down.slug), side: "long" }, ptb, now);
      out.set(dw.window.id, dw);
      continue;
    }

    for (const m of mk) {
      if (cfg.slugRegex && !cfg.slugRegex.test(s(m.slug)) && !cfg.slugRegex.test(s(ev.slug))) continue;
      // several markets in one event = several windows: each needs its own times (event times would be ambiguous)
      const times = extractTimes(m, mk.length === 1 ? ev : null, cfg, now);
      if (!times) continue;
      const slug = s(m.slug);
      const downIsLong = outcomeSide(m) === "down";
      const dw = build(
        slug,
        mk.length === 1 ? title : s(m.title) || title,
        times,
        { slug, side: downIsLong ? "short" : "long" },
        { slug, side: downIsLong ? "long" : "short" },
        extractPriceToBeat(m, mk.length === 1 ? ev : null, cfg.ptbFields),
        now,
      );
      out.set(dw.window.id, dw);
    }
  }
  return [...out.values()].sort((a, b) => a.window.startTs - b.window.startTs);
}

/** Pure: current = live window (latest start if several); next = earliest window starting at/after current end (or after now). */
export function pickCurrentNext<T extends { window: MarketWindow }>(
  windows: T[],
  now: number,
): { current: T | null; next: T | null } {
  let current: T | null = null;
  for (const w of windows) {
    if (w.window.result) continue;
    if (w.window.startTs <= now && now < w.window.endTs) {
      if (!current || w.window.startTs > current.window.startTs) current = w;
    }
  }
  const after = current ? current.window.endTs - DURATION_TOLERANCE_MS : now;
  let next: T | null = null;
  for (const w of windows) {
    if (w === current) continue;
    if (w.window.startTs > now && w.window.startTs >= after) {
      if (!next || w.window.startTs < next.window.startTs) next = w;
    }
  }
  return { current, next };
}

/**
 * Pure: merge a fresh discovery of a window into what we already know. Settlement facts are never lost;
 * a metadata PTB replaces a proxy fallback; status is recomputed for `now`.
 */
export function mergeWindow(prev: DiscoveredWindow | undefined, incoming: DiscoveredWindow, now: number): DiscoveredWindow {
  if (!prev) return incoming;
  const ptbFromMetadata = incoming.ptbFromMetadata || prev.ptbFromMetadata;
  const priceToBeat = incoming.ptbFromMetadata ? incoming.window.priceToBeat : prev.window.priceToBeat;
  const result: WindowResult | null = prev.window.result ?? incoming.window.result;
  const window: MarketWindow = {
    ...incoming.window,
    priceToBeat,
    result,
    settlePrice: prev.window.settlePrice ?? incoming.window.settlePrice,
    status: "upcoming",
  };
  window.status = statusAt(window, now);
  return { ...incoming, window, ptbFromMetadata };
}

export function sameWindow(a: MarketWindow, b: MarketWindow): boolean {
  return (
    a.id === b.id &&
    a.title === b.title &&
    a.startTs === b.startTs &&
    a.endTs === b.endTs &&
    a.priceToBeat === b.priceToBeat &&
    a.upInstrument === b.upInstrument &&
    a.downInstrument === b.downInstrument &&
    a.status === b.status &&
    a.result === b.result &&
    a.settlePrice === b.settlePrice
  );
}

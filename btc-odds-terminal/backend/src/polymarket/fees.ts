// Polymarket US trading fees.
//
// NOT CONFIRMED from an official source: docs.polymarket.us and the CFTC fee filings were unreachable from
// the build sandbox. What IS verified (official SDK types, polymarket-us-typescript src/types/orders.ts):
// every order carries `commissionsBasisPoints` / `makerCommissionsBasisPoints` and every execution carries
// `commissionNotionalCollected` (an Amount), so live fills report their real fee. Third-party write-ups
// disagree (e.g. a taker rate x p x (1-p) curve with a crypto-specific rate vs. flat basis points on premium),
// so no number is baked in: the UI shows [FEE] until the user sets Settings.feeCentsPerContract.
// See docs/POLYMARKET_US.md "Fees".

export const FEE_SCHEDULE: { confirmed: boolean; note: string; source: string | null } = {
  confirmed: false,
  note:
    "Polymarket US fee schedule not confirmed from an official source. Set a fee (cents per contract) in " +
    "settings; live fills use the exchange-reported commissionNotionalCollected.",
  source: null,
};

/**
 * Fee in cents for `contracts` contracts at `priceCents`.
 * `override` = cents per contract (Settings.feeCentsPerContract). Returns null when the schedule is
 * unconfirmed and no override is set (UI then shows [FEE]).
 */
export function feeCents(priceCents: number, contracts: number, override: number | null): number | null {
  if (!Number.isFinite(contracts) || contracts < 0) return null;
  if (override !== null && override !== undefined) {
    if (!Number.isFinite(override) || override < 0) return null;
    return round2(override * contracts);
  }
  if (!FEE_SCHEDULE.confirmed) return null;
  // Placeholder for a confirmed schedule; unreachable while confirmed === false.
  void priceCents;
  return null;
}

/**
 * Helper for a rate-on-variance schedule (fee = C x rate x p x (1-p), p in dollars) should the user want
 * to model one; NOT used by default because the rate is unconfirmed. Result in cents.
 */
export function feeCentsFromRate(priceCents: number, contracts: number, rate: number): number {
  const p = Math.min(Math.max(priceCents / 100, 0), 1);
  return round2(contracts * rate * p * (1 - p) * 100);
}

function round2(v: number): number {
  return Math.round(v * 100) / 100;
}

// Backfill SEED_HOURS of 60s BRTI-proxy history (Coinbase candles) into the bars table.
// Idempotent: INSERT OR IGNORE on (t, d). Usage: npm run seed
// Exits non-zero when a chunk fails or too little history came back, so install.sh retries it later
// (bars from chunks that did arrive are kept: they are inserted chunk by chunk).
import type { Bar } from "../../shared/protocol.ts";
import { loadConfig } from "../src/config.ts";
import { openDb } from "../src/db/index.ts";
import { fetchRecentBars } from "../src/feeds/index.ts";

/** Fail the seed when fewer than this fraction of the expected minutes came back. */
const MIN_COVERAGE = 0.8;

async function main(): Promise<void> {
  const config = loadConfig();
  const db = openDb(config.dbPath);
  let fetched = 0;
  let inserted = 0;
  let first: number | null = null;
  let last: number | null = null;
  try {
    const before = db.countBars(60);
    console.log(`seed: fetching ${config.seedHours}h of 60s bars…`);
    let failure: unknown = null;
    try {
      await fetchRecentBars(config.seedHours, {
        onChunk: (chunk: Bar[]) => {
          const bars = chunk.filter((b) => b.d === 60 && [b.t, b.o, b.h, b.l, b.c].every(Number.isFinite));
          fetched += bars.length;
          inserted += db.insertBarsIgnore(bars);
          for (const b of bars) {
            if (first === null || b.t < first) first = b.t;
            if (last === null || b.t > last) last = b.t;
          }
        },
      });
    } catch (err) {
      failure = err;
    }
    const after = db.countBars(60);
    const iso = (t: number | null) => (t === null ? "-" : new Date(t).toISOString());
    console.log(
      `seed: fetched ${fetched} bars (${iso(first)} → ${iso(last)}); inserted ${inserted}, ` +
        `skipped ${fetched - inserted} existing; 60s bars in db: ${before} → ${after} (${config.dbPath})`,
    );
    if (failure) throw failure;
    const expected = Math.floor(config.seedHours * 60);
    if (fetched === 0 || fetched < expected * MIN_COVERAGE) {
      throw new Error(`only ${fetched} of ~${expected} expected minutes came back`);
    }
  } finally {
    db.close();
  }
}

main().catch((err) => {
  console.error("seed failed:", err instanceof Error ? err.message : err);
  process.exit(1);
});

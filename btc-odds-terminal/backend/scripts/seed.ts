// Backfill SEED_HOURS of 60s BRTI-proxy history (Coinbase candles) into the bars table.
// Idempotent: INSERT OR IGNORE on (t, d). Usage: npm run seed
import { loadConfig } from "../src/config.ts";
import { openDb } from "../src/db/index.ts";
import { fetchRecentBars } from "../src/feeds/index.ts";

async function main(): Promise<void> {
  const config = loadConfig();
  const db = openDb(config.dbPath);
  try {
    const before = db.countBars(60);
    console.log(`seed: fetching ${config.seedHours}h of 60s bars…`);
    const bars = (await fetchRecentBars(config.seedHours)).filter(
      (b) => b.d === 60 && [b.t, b.o, b.h, b.l, b.c].every(Number.isFinite),
    );
    const inserted = db.insertBarsIgnore(bars);
    const after = db.countBars(60);
    const first = bars[0] ? new Date(bars[0].t).toISOString() : "-";
    const last = bars.at(-1) ? new Date(bars.at(-1)!.t).toISOString() : "-";
    console.log(
      `seed: fetched ${bars.length} bars (${first} → ${last}); inserted ${inserted}, ` +
        `skipped ${bars.length - inserted} existing; 60s bars in db: ${before} → ${after} (${config.dbPath})`,
    );
  } finally {
    db.close();
  }
}

main().catch((err) => {
  console.error("seed failed:", err instanceof Error ? err.message : err);
  process.exit(1);
});

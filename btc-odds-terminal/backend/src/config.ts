import "dotenv/config";
import { z } from "zod";

const optionalNumber = z.preprocess(
  (v) => (v === undefined || v === null || (typeof v === "string" && v.trim() === "") ? undefined : v),
  z.coerce.number().finite().nonnegative().optional(),
);

const optionalString = z.preprocess(
  (v) => (typeof v === "string" && v.trim() === "" ? undefined : v),
  z.string().optional(),
);

const EnvSchema = z.object({
  PORT: z.coerce.number().int().min(1).max(65535).default(8787),
  HOST: z.string().default("0.0.0.0"),
  DB_PATH: z.string().default("./data/terminal.db"),
  CORS_ORIGIN: z.string().default("*"),
  TRADING_MODE: z.enum(["paper", "live"]).default("paper"),
  VOL_LOOKBACK_MIN: z.coerce.number().positive().max(24 * 60).default(30),
  FEE_CENTS_PER_CONTRACT: optionalNumber,
  POLY_GATEWAY_URL: z.string().url().default("https://gateway.polymarket.us"),
  POLY_POLL_MS: z.coerce.number().int().min(100).default(1000),
  POLYMARKET_US_KEY_ID: optionalString,
  POLYMARKET_US_SECRET: optionalString,
  SEED_HOURS: z.coerce.number().positive().max(24 * 7).default(6),
  LOG_LEVEL: z.enum(["fatal", "error", "warn", "info", "debug", "trace", "silent"]).default("info"),
});

export interface Config {
  port: number;
  host: string;
  dbPath: string;
  /** "*" or a list of allowed origins */
  corsOrigin: string[] | "*";
  tradingMode: "paper" | "live";
  volLookbackMin: number;
  feeCentsPerContract: number | null;
  polyGatewayUrl: string;
  polyPollMs: number;
  polymarketUsKeyId: string | null;
  polymarketUsSecret: string | null;
  seedHours: number;
  logLevel: string;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const parsed = EnvSchema.safeParse(env);
  if (!parsed.success) {
    const issues = parsed.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`).join("; ");
    throw new Error(`Invalid environment: ${issues}`);
  }
  const e = parsed.data;
  const origins = e.CORS_ORIGIN.split(",").map((s) => s.trim()).filter(Boolean);
  return {
    port: e.PORT,
    host: e.HOST,
    dbPath: e.DB_PATH,
    corsOrigin: origins.length === 0 || origins.includes("*") ? "*" : origins,
    tradingMode: e.TRADING_MODE,
    volLookbackMin: e.VOL_LOOKBACK_MIN,
    feeCentsPerContract: e.FEE_CENTS_PER_CONTRACT ?? null,
    polyGatewayUrl: e.POLY_GATEWAY_URL.replace(/\/+$/, ""),
    polyPollMs: e.POLY_POLL_MS,
    polymarketUsKeyId: e.POLYMARKET_US_KEY_ID ?? null,
    polymarketUsSecret: e.POLYMARKET_US_SECRET ?? null,
    seedHours: e.SEED_HOURS,
    logLevel: e.LOG_LEVEL,
  };
}

import { loadConfig } from "./config.ts";
import { openDb } from "./db/index.ts";
import { Engine } from "./engine.ts";
import { log } from "./log.ts";
import { Recorder } from "./recorder.ts";
import { buildServer } from "./server/index.ts";

async function main(): Promise<void> {
  const config = loadConfig();
  log.level = config.logLevel;
  log.info(
    { port: config.port, host: config.host, db: config.dbPath, mode: config.tradingMode, cors: config.corsOrigin },
    "BTC Odds Terminal backend starting",
  );
  if (config.corsOrigin === "*") {
    log.warn("CORS_ORIGIN=* lets any web page in your browser call this API (including PUT /api/settings); list your frontend origin(s) instead");
  }

  const db = openDb(config.dbPath);
  log.info({ schemaVersion: db.schemaVersion }, "database ready");

  const recorder = new Recorder(db, { logger: log.child({ component: "recorder" }) });
  const engine = new Engine({ config, db, recorder, logger: log });
  const app = buildServer(engine, db, config, { logger: log.child({ component: "http" }) });

  let shuttingDown = false;
  const shutdown = async (signal: string, code = 0): Promise<void> => {
    if (shuttingDown) {
      log.warn({ signal }, "forced exit");
      process.exit(1);
    }
    shuttingDown = true;
    log.info({ signal }, "shutting down");
    const force = setTimeout(() => {
      log.error("shutdown timed out, exiting");
      process.exit(1);
    }, 10_000);
    force.unref();
    try {
      await app.close();
    } catch (err) {
      log.error({ err }, "http close failed");
    }
    try {
      engine.stop(); // stops feeds and flushes the recorder buffer
    } catch (err) {
      log.error({ err }, "engine stop failed");
    }
    try {
      db.close();
    } catch (err) {
      log.error({ err }, "db close failed");
    }
    log.info("bye");
    process.exit(code);
  };

  process.on("SIGINT", () => void shutdown("SIGINT"));
  process.on("SIGTERM", () => void shutdown("SIGTERM"));
  process.on("unhandledRejection", (err) => log.error({ err }, "unhandled rejection"));
  process.on("uncaughtException", (err) => {
    log.fatal({ err }, "uncaught exception");
    void shutdown("uncaughtException", 1);
  });

  engine.start();
  await app.listen({ port: config.port, host: config.host });
  log.info({ url: `http://${config.host}:${config.port}`, ws: "/ws" }, "listening");
}

main().catch((err) => {
  log.fatal({ err }, "startup failed");
  process.exit(1);
});

import pino from "pino";

export type Logger = Pick<pino.Logger, "info" | "warn" | "error" | "debug" | "child">;

export const log: pino.Logger = pino({
  level: process.env.LOG_LEVEL ?? "info",
  transport: process.stdout.isTTY ? { target: "pino-pretty", options: { translateTime: "SYS:HH:MM:ss" } } : undefined,
});

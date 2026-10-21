// SQLite persistence for alerts / alert_events (tables from migration v2). OWNER: alerts-risk.

import type Database from "better-sqlite3";
import type { Alert, AlertEvent, AlertKind } from "../../../shared/protocol.ts";

/** alert_events older than this are pruned (start-up + periodically). */
export const ALERT_EVENTS_RETENTION_MS = 30 * 86_400_000;
/** ...and at most this many rows are kept. */
export const ALERT_EVENTS_MAX_ROWS = 10_000;

interface AlertRow {
  id: string;
  kind: string;
  threshold: number | null;
  enabled: number;
  once: number;
  note: string | null;
  last_fired_ts: number | null;
  created_ts: number;
  updated_ts: number;
}

interface EventRow {
  id: number;
  alert_id: string;
  kind: string;
  ts: number;
  window_id: string | null;
  value: number | null;
  message: string;
}

const toAlert = (r: AlertRow): Alert => ({
  id: r.id,
  kind: r.kind as AlertKind,
  threshold: r.threshold,
  enabled: r.enabled === 1,
  once: r.once === 1,
  note: r.note,
  lastFiredTs: r.last_fired_ts,
  createdTs: r.created_ts,
  updatedTs: r.updated_ts,
});

const toEvent = (r: EventRow): AlertEvent => ({
  id: r.id,
  alertId: r.alert_id,
  kind: r.kind as AlertKind,
  ts: r.ts,
  windowId: r.window_id,
  value: r.value,
  message: r.message,
});

const alertParams = (a: Alert) => ({
  id: a.id,
  kind: a.kind,
  threshold: a.threshold,
  enabled: a.enabled ? 1 : 0,
  once: a.once ? 1 : 0,
  note: a.note,
  last_fired_ts: a.lastFiredTs,
  created_ts: a.createdTs,
  updated_ts: a.updatedTs,
});

export class AlertStore {
  private readonly s;

  constructor(private readonly db: Database.Database) {
    this.s = {
      all: db.prepare<[], AlertRow>("SELECT * FROM alerts ORDER BY created_ts ASC, id ASC"),
      insert: db.prepare(
        `INSERT INTO alerts (id, kind, threshold, enabled, once, note, last_fired_ts, created_ts, updated_ts)
         VALUES (@id, @kind, @threshold, @enabled, @once, @note, @last_fired_ts, @created_ts, @updated_ts)`,
      ),
      update: db.prepare(
        `UPDATE alerts SET kind = @kind, threshold = @threshold, enabled = @enabled, once = @once, note = @note,
           last_fired_ts = @last_fired_ts, updated_ts = @updated_ts WHERE id = @id`,
      ),
      remove: db.prepare<[string]>("DELETE FROM alerts WHERE id = ?"),
      insertEvent: db.prepare(
        `INSERT INTO alert_events (alert_id, kind, ts, window_id, value, message)
         VALUES (@alertId, @kind, @ts, @windowId, @value, @message)`,
      ),
      events: db.prepare<[number], EventRow>("SELECT * FROM alert_events ORDER BY id DESC LIMIT ?"),
      eventsFor: db.prepare<[string, number], EventRow>("SELECT * FROM alert_events WHERE alert_id = ? ORDER BY id DESC LIMIT ?"),
      pruneOld: db.prepare<[number]>("DELETE FROM alert_events WHERE ts < ?"),
      pruneRows: db.prepare<[number]>(
        "DELETE FROM alert_events WHERE id <= (SELECT id FROM alert_events ORDER BY id DESC LIMIT 1 OFFSET ?)",
      ),
    };
  }

  all(): Alert[] {
    return this.s.all.all().map(toAlert);
  }

  insert(a: Alert): void {
    this.s.insert.run(alertParams(a));
  }

  update(a: Alert): void {
    this.s.update.run(alertParams(a));
  }

  remove(id: string): boolean {
    return this.s.remove.run(id).changes > 0;
  }

  /** Insert an event and return it with its row id. */
  insertEvent(e: Omit<AlertEvent, "id">): AlertEvent {
    const r = this.s.insertEvent.run(e);
    return { id: Number(r.lastInsertRowid), ...e };
  }

  events(limit: number, alertId?: string): AlertEvent[] {
    return (alertId ? this.s.eventsFor.all(alertId, limit) : this.s.events.all(limit)).map(toEvent);
  }

  /** Drop events older than the retention and beyond the row cap; returns rows deleted. */
  prune(now: number): number {
    return this.tx(() => this.s.pruneOld.run(now - ALERT_EVENTS_RETENTION_MS).changes + this.s.pruneRows.run(ALERT_EVENTS_MAX_ROWS).changes);
  }

  tx<T>(fn: () => T): T {
    return this.db.transaction(fn)();
  }
}

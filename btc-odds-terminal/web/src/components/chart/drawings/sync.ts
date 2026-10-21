// Debounced persistence of local drawing edits to /api/drawings.
//
// Local edits (commit / undo / redo, run through `local()`) are recorded per drawing id: an upsert of its new
// version or a deletion. The chart always shows the server list as last seen with those edits laid over it,
// merged by id — so drawings this client never saw (another tab, a snapshot after a reconnect, the first
// load) are kept, never overwritten by a stale local list. Requests are per drawing:
//   new drawing   -> POST /api/drawings (the server's id replaces the client id; see onRemap)
//   edit          -> PUT /api/drawings/:id (404: deleted elsewhere meanwhile -> re-created by POST)
//   deletion      -> DELETE /api/drawings/:id (404 is fine)
// Replace-all (PUT /api/drawings) is only used to restore many drawings at once (undo of "delete all"), and
// DELETE /api/drawings only when every drawing on the server is being deleted.
//
// Nothing is sent before the server list is known (first `drawings` snapshot / echo, or GET /api/drawings).
// Network errors, timeouts, 429 and 5xx retry with backoff, up to `maxAttempts`, then the queue goes
// "offline" (kept, retried on the next edit, the next server list, or retry()). Any other error (4xx, 501)
// rejects that change: it is dropped and the drawing goes back to the server's version.
// Dependencies are injected so the queue is unit-testable without the store or the network.

import type { Drawing, DrawingInput } from "@shared/protocol";
import { diffLists } from "./history";
import { inputOf, sameInput, validDrawings } from "./model";

export interface SyncApi {
  list(): Promise<Drawing[]>;
  createDrawing(input: DrawingInput): Promise<Drawing>;
  replaceDrawings(list: Drawing[]): Promise<unknown>;
  updateDrawing(id: string, patch: Partial<DrawingInput>): Promise<unknown>;
  deleteDrawing(id: string): Promise<unknown>;
  deleteAllDrawings(): Promise<unknown>;
}

export interface SyncStore {
  get(): Drawing[];
  /** replace the list without touching undo history (store.setDrawings) */
  set(list: Drawing[]): void;
  subscribe(fn: (list: Drawing[]) => void): () => void;
}

/**
 * loading  — waiting for the first server list (edits are held)
 * saved    — everything is on the server
 * saving   — edits queued / in flight
 * retrying — the last attempt failed with a transient error; another is scheduled
 * offline  — gave up retrying; edits are kept on this page only (lost on reload)
 * rejected — the server refused a change; it was reverted (sticky until the next clean save / dismiss)
 */
export type SyncStatus = "loading" | "saved" | "saving" | "retrying" | "offline" | "rejected";

export interface SyncOptions {
  debounceMs?: number;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (h: unknown) => void;
  now?: () => number;
  /** the store already holds the server list (skips the initial GET) */
  hydrated?: boolean;
  /** consecutive transient failures before going offline */
  maxAttempts?: number;
  onStatus?: (status: SyncStatus, err: unknown) => void;
  /** the server assigned `to` to the drawing created locally as `from` (selection / history / editor follow) */
  onRemap?: (from: string, to: string) => void;
}

/** id -> new version (null = deleted) */
export type Overlay = Map<string, Drawing | null>;

export type SyncReq =
  | { kind: "create"; id: string; d: Drawing }
  | { kind: "update"; id: string; d: Drawing }
  | { kind: "delete"; id: string }
  | { kind: "clear"; ids: string[] }
  | { kind: "replace"; list: Drawing[]; ids: string[] };

/** above this many POSTs a single replace-all is used (bulk restore) */
export const MAX_OPS = 6;
export const DEBOUNCE_MS = 350;
export const MAX_ATTEMPTS = 6;
/** how long a confirmed write overrides older server lists (stale echoes, by server updatedTs) */
export const CONFIRM_TTL_MS = 5000;
const BACKOFF_MS = [1000, 3000, 10000, 30000];
const backoff = (attempt: number) => BACKOFF_MS[Math.min(BACKOFF_MS.length - 1, Math.max(0, attempt - 1))];

export const statusOf = (e: unknown): number | null => {
  const s = e && typeof e === "object" && "status" in e ? (e as { status: unknown }).status : null;
  return typeof s === "number" ? s : null;
};

/** network error / timeout / abort, 408, 425, 429 and 5xx (except 501 Not Implemented) are worth retrying */
export function isRetryable(e: unknown): boolean {
  const s = statusOf(e);
  if (s == null || s === 0) return true;
  return s === 408 || s === 425 || s === 429 || (s >= 500 && s !== 501);
}

/** same ids in the same order with the same content */
export function sameList(a: readonly Drawing[], b: readonly Drawing[]): boolean {
  return a.length === b.length && a.every((d, i) => d.id === b[i].id && sameInput(d, b[i]));
}

/** same drawings (by id and content), in any order */
function sameSet(a: readonly Drawing[], b: readonly Drawing[]): boolean {
  if (a.length !== b.length) return false;
  const m = new Map(a.map((d) => [d.id, d]));
  return m.size === b.length && b.every((d) => {
    const x = m.get(d.id);
    return !!x && (x === d || sameInput(x, d));
  });
}

/** `server` with the overlays laid over it by id (later overlays win); upserts the server lacks are appended */
export function mergeLists(server: readonly Drawing[], ...overlays: Overlay[]): Drawing[] {
  const top: Overlay = new Map();
  for (const o of overlays) for (const [id, d] of o) top.set(id, d);
  const out: Drawing[] = [];
  const seen = new Set<string>();
  for (const d of server) {
    if (seen.has(d.id)) continue;
    seen.add(d.id);
    if (!top.has(d.id)) out.push(d);
    else {
      const t = top.get(d.id);
      if (t) out.push(t);
    }
  }
  for (const [id, d] of top) {
    if (d && !seen.has(id)) {
      seen.add(id);
      out.push(d);
    }
  }
  return out;
}

/** the requests that turn the server list `known` into `known` + `ops` */
export function planBatch(known: readonly Drawing[], ops: Overlay): SyncReq[] {
  const byId = new Map(known.map((d) => [d.id, d]));
  const creates: SyncReq[] = [];
  const updates: SyncReq[] = [];
  const deletes: string[] = [];
  for (const [id, d] of ops) {
    const s = byId.get(id);
    if (!d) {
      if (s) deletes.push(id);
    } else if (!s) creates.push({ kind: "create", id, d });
    else if (!sameInput(s, d)) updates.push({ kind: "update", id, d });
  }
  if (creates.length > MAX_OPS) return [{ kind: "replace", list: mergeLists(known, ops), ids: [...ops.keys()] }];
  if (deletes.length > MAX_OPS && deletes.length === byId.size && !creates.length && !updates.length) return [{ kind: "clear", ids: deletes }];
  return [...deletes.map((id): SyncReq => ({ kind: "delete", id })), ...updates, ...creates];
}

/** a planned request as plain HTTP (path relative to /api), for keepalive sends while the page unloads */
export function reqHttp(r: SyncReq): { method: "POST" | "PUT" | "DELETE"; path: string; body?: unknown } {
  const enc = encodeURIComponent;
  switch (r.kind) {
    case "create":
      return { method: "POST", path: "/drawings", body: inputOf(r.d) };
    case "update":
      return { method: "PUT", path: `/drawings/${enc(r.id)}`, body: inputOf(r.d) };
    case "delete":
      return { method: "DELETE", path: `/drawings/${enc(r.id)}` };
    case "clear":
      return { method: "DELETE", path: "/drawings" };
    case "replace":
      return { method: "PUT", path: "/drawings", body: r.list };
  }
}

const idsOf = (r: SyncReq): string[] => (r.kind === "clear" || r.kind === "replace" ? r.ids : [r.id]);

export class DrawingSync {
  /** last list known to be stored on the server */
  private persisted: Drawing[];
  /** writes the server confirmed, kept over older server lists for a few seconds */
  private confirmed = new Map<string, { d: Drawing | null; until: number }>();
  /** local edits waiting for the debounce */
  private queued: Overlay = new Map();
  /** edits being written */
  private inflight: Overlay | null = null;
  private hydrated: boolean;
  private loading = false;
  private loadAttempt = 0;
  private loadTimer: unknown = null;
  private timer: unknown = null;
  private localDepth = 0;
  private attempt = 0;
  private rejected = false;
  private lastError: unknown = null;
  private current: SyncStatus;
  private unsub: (() => void) | null = null;
  private idleWaiters: (() => void)[] = [];
  private readonly debounceMs: number;
  private readonly maxAttempts: number;
  private readonly setTimer: (fn: () => void, ms: number) => unknown;
  private readonly clearTimer: (h: unknown) => void;
  private readonly now: () => number;

  constructor(
    private readonly api: SyncApi,
    private readonly store: SyncStore,
    private readonly opts: SyncOptions = {},
  ) {
    this.debounceMs = opts.debounceMs ?? DEBOUNCE_MS;
    this.maxAttempts = Math.max(1, opts.maxAttempts ?? MAX_ATTEMPTS);
    this.setTimer = opts.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer = opts.clearTimer ?? ((h) => clearTimeout(h as ReturnType<typeof setTimeout>));
    this.now = opts.now ?? Date.now;
    this.persisted = store.get();
    this.hydrated = !!opts.hydrated;
    this.current = this.hydrated ? "saved" : "loading";
  }

  start(): this {
    if (this.unsub) return this;
    this.persisted = this.store.get();
    this.unsub = this.store.subscribe((list) => this.onStore(list));
    if (!this.hydrated) void this.load();
    return this;
  }

  stop() {
    this.unsub?.();
    this.unsub = null;
    if (this.timer != null) this.clearTimer(this.timer);
    if (this.loadTimer != null) this.clearTimer(this.loadTimer);
    this.timer = null;
    this.loadTimer = null;
  }

  /** the server list is known: edits can be made and saved */
  ready(): boolean {
    return this.hydrated;
  }

  status(): SyncStatus {
    return this.current;
  }

  error(): unknown {
    return this.lastError;
  }

  /** true while local edits are not yet confirmed by the server */
  pending(): boolean {
    return this.queued.size > 0 || this.inflight != null;
  }

  /** Run a local store write (commit / undo / redo) and queue the drawings it changed for saving. */
  local<T>(fn: () => T): T {
    const before = this.store.get();
    this.localDepth++;
    let out: T;
    try {
      out = fn();
    } finally {
      this.localDepth--;
    }
    const changes = diffLists(before, this.store.get());
    for (const c of changes) this.queued.set(c.id, c.after);
    if (changes.length) {
      if (this.attempt >= this.maxAttempts) this.attempt = 0; // offline: a new edit tries again
      this.schedule(this.debounceMs);
    }
    this.refreshStatus();
    return out;
  }

  /** Send any pending edit now (skips the debounce). */
  flushNow(): Promise<void> {
    if (this.timer != null) {
      this.clearTimer(this.timer);
      this.timer = null;
    }
    void this.flush();
    return this.whenIdle();
  }

  /** retry now after failures (resets the attempt counter) */
  retry(): Promise<void> {
    this.attempt = 0;
    if (!this.hydrated) {
      this.loadAttempt = 0;
      void this.load();
    }
    return this.flushNow();
  }

  /** acknowledge a rejected change (status back to saved / saving) */
  dismissError() {
    this.rejected = false;
    this.refreshStatus();
  }

  /** resolves once nothing is pending or in flight (or saving gave up: offline) */
  whenIdle(): Promise<void> {
    if (this.idle()) return Promise.resolve();
    return new Promise((r) => this.idleWaiters.push(r));
  }

  /**
   * The requests that would save what is still unsent, for a keepalive send while the page unloads.
   * Creations already in flight are left out (POST is not idempotent). The returned requests are
   * treated as sent.
   */
  takeUnloadRequests(): SyncReq[] {
    if (!this.hydrated || !this.pending()) return [];
    const ops: Overlay = new Map(this.inflight ?? []);
    for (const [id, d] of this.queued) ops.set(id, d);
    const known = this.known();
    const inflightOnly = (id: string) => !this.queued.has(id) && !!this.inflight?.has(id);
    const reqs = planBatch(known, ops).filter((r) => !(r.kind === "create" && inflightOnly(r.id)));
    if (this.timer != null) this.clearTimer(this.timer);
    this.timer = null;
    const until = this.now() + CONFIRM_TTL_MS;
    for (const r of reqs) for (const id of idsOf(r)) this.confirmed.set(id, { d: ops.get(id) ?? null, until });
    this.queued = new Map();
    this.refreshStatus();
    return reqs;
  }

  // ---- internals ------------------------------------------------------------------------------------

  private idle(): boolean {
    return (!this.pending() && this.timer == null) || this.current === "offline";
  }

  private onStore(list: Drawing[]) {
    if (this.localDepth > 0) return;
    this.applyServer(list);
  }

  /** a server list (snapshot / echo / GET) */
  private applyServer(list: Drawing[]) {
    this.persisted = list;
    const wasHydrated = this.hydrated;
    this.hydrated = true;
    if (this.loadTimer != null) this.clearTimer(this.loadTimer);
    this.loadTimer = null;
    this.pruneConfirmed(list);
    this.render();
    if (this.queued.size && this.timer == null && !this.inflight) {
      // first server list: send what was held; offline: the server answers again, so try again
      if (!wasHydrated) this.schedule(this.debounceMs);
      else if (this.attempt >= this.maxAttempts) {
        this.attempt = 0;
        this.schedule(this.debounceMs);
      }
    }
    this.refreshStatus();
  }

  private async load(): Promise<void> {
    if (this.hydrated || this.loading) return;
    this.loading = true;
    this.loadTimer = null;
    try {
      const list = await this.api.list();
      this.loading = false;
      if (this.hydrated || !this.unsub) return;
      this.applyServer(validDrawings(Array.isArray(list) ? list : []) as Drawing[]);
    } catch (e) {
      this.loading = false;
      if (this.hydrated || !this.unsub) return;
      if (isRetryable(e)) {
        this.loadAttempt++;
        this.loadTimer = this.setTimer(() => void this.load(), backoff(this.loadAttempt));
        return;
      }
      // the server cannot list drawings (e.g. not implemented): start from what the store has and say so
      this.rejected = true;
      this.lastError = e;
      this.applyServer(this.store.get());
    }
  }

  private confirmedOverlay(): Overlay {
    const now = this.now();
    const o: Overlay = new Map();
    for (const [id, c] of this.confirmed) {
      if (c.until > now) o.set(id, c.d);
      else this.confirmed.delete(id);
    }
    return o;
  }

  /** the server list as last seen, with the writes it confirmed since */
  private known(): Drawing[] {
    return mergeLists(this.persisted, this.confirmedOverlay());
  }

  /**
   * Drop confirmed writes a server list has caught up with: it shows the same content, a newer version
   * (higher server updatedTs: someone edited it since), or the deletion.
   */
  private pruneConfirmed(list: readonly Drawing[]) {
    const now = this.now();
    const byId = new Map(list.map((d) => [d.id, d]));
    for (const [id, c] of this.confirmed) {
      const s = byId.get(id);
      const caughtUp = c.d ? !!s && (sameInput(s, c.d) || (s.updatedTs ?? 0) > (c.d.updatedTs ?? 0)) : !s;
      if (c.until <= now || caughtUp) this.confirmed.delete(id);
    }
  }

  private confirm(id: string, d: Drawing | null) {
    this.confirmed.set(id, { d, until: this.now() + CONFIRM_TTL_MS });
  }

  /** what the chart shows: the server list with confirmed, in-flight and queued edits laid over it */
  private view(): Drawing[] {
    return validDrawings(mergeLists(this.persisted, this.confirmedOverlay(), this.inflight ?? new Map(), this.queued)) as Drawing[];
  }

  /** push the merged view into the store when it differs (order-only differences are kept as they are) */
  private render() {
    const view = this.view();
    if (sameSet(view, this.store.get())) return;
    this.writeStore(view);
  }

  private writeStore(list: Drawing[]) {
    this.localDepth++;
    try {
      this.store.set(list);
    } finally {
      this.localDepth--;
    }
  }

  private schedule(ms: number) {
    if (this.timer != null) this.clearTimer(this.timer);
    this.timer = this.setTimer(() => {
      this.timer = null;
      void this.flush();
    }, ms);
  }

  private async flush(): Promise<void> {
    if (this.inflight || !this.hydrated) {
      this.refreshStatus();
      return; // the running batch / the first server list reschedules
    }
    if (!this.queued.size) {
      this.refreshStatus();
      this.settle();
      return;
    }
    const batch = this.queued;
    this.queued = new Map();
    this.inflight = batch;
    this.refreshStatus();
    const reqs = planBatch(this.known(), batch);
    let retryErr: unknown = null;
    let fatalErr: unknown = null;
    let unsent: string[] = [];
    for (let k = 0; k < reqs.length; k++) {
      const r = reqs[k];
      try {
        await this.run(r);
      } catch (e) {
        if (isRetryable(e)) {
          retryErr = e;
          unsent = reqs.slice(k).flatMap(idsOf);
          break;
        }
        fatalErr = e;
        // rejected: drop the change (and later edits of the same drawing) so it shows the server's version
        for (const id of idsOf(r)) this.queued.delete(id);
      }
    }
    this.inflight = null;
    if (retryErr) {
      // put the unsent edits back in front of anything queued meanwhile (a newer edit of a drawing wins)
      const back: Overlay = new Map();
      for (const id of unsent) if (batch.has(id)) back.set(id, batch.get(id) ?? null);
      for (const [id, d] of this.queued) back.set(id, d);
      this.queued = back;
      this.attempt++;
      this.lastError = retryErr;
      if (this.attempt < this.maxAttempts) this.schedule(backoff(this.attempt));
    } else {
      this.attempt = 0;
      if (reqs.length && !fatalErr) this.rejected = false;
      if (this.queued.size) this.schedule(this.debounceMs);
    }
    if (fatalErr) {
      this.rejected = true;
      this.lastError = fatalErr;
    }
    this.render();
    this.refreshStatus();
    this.settle();
  }

  private async run(r: SyncReq): Promise<void> {
    switch (r.kind) {
      case "create": {
        const srv = await this.api.createDrawing(inputOf(r.d));
        const id = srv && typeof srv.id === "string" && srv.id ? srv.id : r.id;
        const saved: Drawing = {
          ...r.d,
          id,
          createdTs: srv?.createdTs ?? r.d.createdTs,
          updatedTs: Number.isFinite(srv?.updatedTs) ? srv.updatedTs : 0,
        };
        this.persisted = [...this.persisted.filter((d) => d.id !== id), saved];
        this.confirm(id, saved);
        if (id !== r.id) this.remap(r.id, id);
        return;
      }
      case "update": {
        let srv: unknown;
        try {
          srv = await this.api.updateDrawing(r.id, inputOf(r.d));
        } catch (e) {
          if (statusOf(e) !== 404) throw e;
          // deleted elsewhere while this edit was pending: re-create it rather than lose the edit
          return this.run({ kind: "create", id: r.id, d: r.d });
        }
        // the server's updatedTs tells a later server list whether it is older or newer than this write
        const ts = srv && typeof srv === "object" && Number.isFinite((srv as Drawing).updatedTs) ? (srv as Drawing).updatedTs : 0;
        const saved: Drawing = { ...r.d, updatedTs: ts };
        this.persisted = this.persisted.some((d) => d.id === r.id) ? this.persisted.map((d) => (d.id === r.id ? saved : d)) : [...this.persisted, saved];
        this.confirm(r.id, saved);
        return;
      }
      case "delete":
        try {
          await this.api.deleteDrawing(r.id);
        } catch (e) {
          if (statusOf(e) !== 404) throw e; // already gone
        }
        this.persisted = this.persisted.filter((d) => d.id !== r.id);
        this.confirm(r.id, null);
        return;
      case "clear":
        await this.api.deleteAllDrawings();
        this.persisted = [];
        for (const id of r.ids) this.confirm(id, null);
        return;
      case "replace": {
        await this.api.replaceDrawings(r.list);
        this.persisted = r.list;
        const byId = new Map(r.list.map((d) => [d.id, d]));
        for (const id of r.ids) this.confirm(id, byId.get(id) ?? null);
        return;
      }
    }
  }

  /** the drawing created as `from` is `to` on the server */
  private remap(from: string, to: string) {
    if (this.queued.has(from)) {
      const d = this.queued.get(from) ?? null;
      this.queued.delete(from);
      this.queued.set(to, d ? { ...d, id: to } : null);
    }
    this.opts.onRemap?.(from, to);
    const cur = this.store.get();
    if (cur.some((d) => d.id === from)) {
      // rename in place; drop a copy the server echo may already have added
      this.writeStore(cur.filter((d) => d.id !== to).map((d) => (d.id === from ? { ...d, id: to } : d)));
    }
  }

  private refreshStatus() {
    const pending = this.pending() || this.timer != null;
    const next: SyncStatus = !this.hydrated
      ? "loading"
      : pending && this.attempt >= this.maxAttempts
        ? "offline"
        : pending && this.attempt > 0
          ? "retrying"
          : pending
            ? "saving"
            : this.rejected
              ? "rejected"
              : "saved";
    if (next === this.current) return;
    this.current = next;
    this.opts.onStatus?.(next, next === "retrying" || next === "offline" || next === "rejected" ? this.lastError : null);
  }

  private settle() {
    if (!this.idle()) return;
    const w = this.idleWaiters;
    this.idleWaiters = [];
    for (const f of w) f();
  }
}

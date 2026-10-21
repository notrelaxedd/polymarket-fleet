// Debounced, serialized bracket writes for chart drags. While a TP/SL line is dragged the chart writes the
// new values to store.bracketDrafts[positionId] (so every surface shows them at once) and asks this class to
// persist them: PUT /api/positions/:id/bracket at most once per `debounceMs`, never two in flight for the
// same position, always the latest draft. Once the server has the draft and nothing newer is pending, the
// draft is dropped again (only if it is still the very object this class sent: an Edit bracket dialog that
// writes its own draft meanwhile is left alone). A draft equal to the server's bracket is not sent at all.
// dispose() (chart remount) does not drop work: pending writes go out at once and in-flight ones still report
// their result and drop their draft, so no draft outlives the controller that made it.

import type { Bracket, BracketInput } from "@shared/protocol";
import { bracketEmpty } from "@shared/paper";
import { sameBracket } from "./model";

export interface BracketSyncDeps {
  put(positionId: string, bracket: BracketInput | null): Promise<Bracket | null>;
  /** the draft to persist right now (store.bracketDrafts[positionId]) */
  getDraft(positionId: string): BracketInput | undefined;
  /** the server's current bracket of the position (store, active only); null = none */
  getServer(positionId: string): BracketInput | null;
  /** drop the draft if it is still `sent` (identity) */
  clearDraft(positionId: string, sent: BracketInput): void;
  /** the server accepted `sent`; `saved` is its bracket (null = removed) */
  onSaved(positionId: string, saved: Bracket | null, sent: BracketInput): void;
  onError(positionId: string, err: unknown): void;
  debounceMs?: number;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (t: unknown) => void;
}

interface Slot {
  timer: unknown;
  inFlight: boolean;
  /** a newer draft arrived while a PUT was in flight */
  dirty: boolean;
  /** the user still holds the handle: keep the draft even when the server caught up */
  holding: boolean;
  /** last draft object the server accepted */
  saved: BracketInput | null;
}

export const BRACKET_DEBOUNCE_MS = 250;

export class BracketSync {
  private readonly slots = new Map<string, Slot>();
  private readonly debounceMs: number;
  private readonly setTimer: (fn: () => void, ms: number) => unknown;
  private readonly clearTimer: (t: unknown) => void;
  private disposed = false;

  constructor(private readonly d: BracketSyncDeps) {
    this.debounceMs = d.debounceMs ?? BRACKET_DEBOUNCE_MS;
    this.setTimer = d.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer = d.clearTimer ?? ((t) => clearTimeout(t as ReturnType<typeof setTimeout>));
  }

  private slot(id: string): Slot {
    let s = this.slots.get(id);
    if (!s) {
      s = { timer: null, inFlight: false, dirty: false, holding: false, saved: null };
      this.slots.set(id, s);
    }
    return s;
  }

  /** The user grabbed a handle of this position (keeps the draft alive until release). */
  hold(id: string) {
    this.slot(id).holding = true;
  }

  /** The handle was released; flushes whatever is pending. */
  release(id: string) {
    const s = this.slots.get(id);
    if (!s) return;
    s.holding = false;
    this.flush(id);
  }

  /** Persist the current draft after the debounce (restarts it). */
  schedule(id: string) {
    if (this.disposed) return;
    const s = this.slot(id);
    if (s.timer != null) this.clearTimer(s.timer);
    s.timer = this.setTimer(() => {
      s.timer = null;
      this.send(id);
    }, this.debounceMs);
  }

  /** Persist the current draft now. */
  flush(id: string) {
    if (this.disposed) return;
    const s = this.slot(id);
    if (s.timer != null) {
      this.clearTimer(s.timer);
      s.timer = null;
    }
    this.send(id);
  }

  /** True while a write for this position is pending or in flight. */
  busy(id: string): boolean {
    const s = this.slots.get(id);
    return !!s && (s.inFlight || s.timer != null || s.dirty);
  }

  /** Positions with pending / in-flight work or a held handle. */
  ids(): string[] {
    return [...this.slots.keys()];
  }

  /** Drop a position's pending work (closed position). Its in-flight result is ignored. */
  forget(id: string) {
    const s = this.slots.get(id);
    if (!s) return;
    if (s.timer != null) this.clearTimer(s.timer);
    this.slots.delete(id);
  }

  /**
   * No new work is accepted; pending writes are sent now, holds released, and every in-flight write still
   * reports (onSaved / onError) and settles its draft.
   */
  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    for (const [id, s] of [...this.slots]) {
      s.holding = false;
      if (s.timer != null) {
        this.clearTimer(s.timer);
        s.timer = null;
      }
      this.send(id);
    }
  }

  private send(id: string) {
    const s = this.slots.get(id);
    if (!s) return;
    if (s.inFlight) {
      s.dirty = true;
      return;
    }
    const draft = this.d.getDraft(id);
    if (!draft) {
      // nothing to write (handle grabbed and released without a change, or the draft was dropped)
      if (!s.holding && s.timer == null) this.slots.delete(id);
      return;
    }
    if (draft === s.saved || this.matchesServer(id, draft)) {
      // nothing to change on the server (e.g. Esc restored the original, or a nudge went back and forth)
      s.saved = draft;
      this.settle(id, s, draft);
      return;
    }
    s.inFlight = true;
    s.dirty = false;
    this.d
      .put(id, bracketEmpty(draft) ? null : draft)
      .then(
        (saved) => {
          if (this.slots.get(id) !== s) return;
          s.saved = draft;
          this.d.onSaved(id, saved, draft);
        },
        (err) => {
          if (this.slots.get(id) !== s) return;
          // a failed write drops what was queued behind it too: the caller reverts the draft
          s.dirty = false;
          if (s.timer != null) {
            this.clearTimer(s.timer);
            s.timer = null;
          }
          this.d.onError(id, err);
        },
      )
      .finally(() => {
        s.inFlight = false;
        if (this.slots.get(id) !== s) return;
        if (s.dirty) {
          s.dirty = false;
          this.send(id);
        } else if (s.saved === draft) this.settle(id, s, draft);
      });
  }

  private matchesServer(id: string, draft: BracketInput): boolean {
    const server = this.d.getServer(id);
    if (bracketEmpty(draft)) return bracketEmpty(server);
    return server != null && sameBracket(draft, server);
  }

  /** Server is up to date with `draft`: drop the draft unless more is coming. */
  private settle(id: string, s: Slot, draft: BracketInput) {
    if (s.holding || s.timer != null || s.inFlight || s.dirty) return;
    this.d.clearDraft(id, draft);
    this.slots.delete(id);
  }
}

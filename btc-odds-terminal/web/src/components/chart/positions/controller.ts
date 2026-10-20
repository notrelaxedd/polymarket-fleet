// Glue between the store, the canvas primitive, the HTML labels and the bracket writes.
//  - builds the lines from the store (open positions of the current window + the ticket's bracket preview);
//  - a TP/SL drag writes store.setBracketDraft(positionId, …) on every move (all surfaces show the new value
//    and the projected P&L at once) and BracketSync PUTs it, debounced; release flushes, Esc reverts;
//  - ✕ turns a leg off (PUT at once); "+TP" / "+SL" on the entry label adds a leg at a sensible distance;
//  - ticket preview legs (before entry) write store.setTicket({ tpCents | slCents }) instead, no request;
//  - nothing is shown (and any drag is dropped) while a replay is active: positions are live state;
//  - a position's legs move / are added only against a FRESH bid of its side (quoteBids): no quote or a
//    stale one locks them, so a leg is never saved where it would fire at once.

import { STALE_AFTER_MS, type Bracket, type BracketInput, type Side } from "@shared/protocol";
import { sideBook } from "@shared/paper";
import { api } from "@/lib/api";
import { isApiError } from "@/lib/api-error";
import { liveNow, selectOdds, useTerminal, type TerminalState } from "@/lib/store";
import { estimateTicket, parseNum } from "@/components/panels/ticket-math";
import type { ChartHandle } from "../context";
import { BracketSync } from "./bracketSync";
import { LabelOverlay } from "./LabelOverlay";
import {
  bracketInput,
  bracketOf,
  buildLines,
  defaultLeg,
  draftLegCents,
  exitingBracket,
  nudgeCents,
  positionLegBounds,
  savedBracketUpdates,
  snapCents,
  withLeg,
  type LegKind,
  type PositionLine,
  type TicketPreview,
} from "./model";
import type { PositionLinesPrimitive } from "./PositionLinesPrimitive";

interface DragState {
  key: string;
  draft: boolean;
  positionId: string | null;
  kind: LegKind;
  /** position: bracket before the drag (Esc restores it) */
  orig: BracketInput | null;
  /** ticket preview: text before the drag */
  origText: string;
  moved: boolean;
}

/** The ticket's bracket preview (null when off / nothing to draw). */
export function ticketPreview(st: TerminalState): TicketPreview | null {
  const t = st.ticket;
  if (!t.bracketOn || !st.current) return null;
  const tp = draftLegCents(t.tpOn, t.tpCents);
  const sl = draftLegCents(t.slOn, t.slCents);
  if (tp == null && sl == null) return null;
  const limit = parseNum(t.limitCents);
  const book = st.book && st.book.windowId === st.current.id ? st.book : null;
  const est = estimateTicket({
    side: t.side,
    type: t.type,
    usd: parseNum(t.usd),
    limitCents: limit,
    maxSlippageCents: parseNum(t.maxSlippageCents),
    book,
    odds: selectOdds(st),
    fairUp: null,
    feeCentsPerContract: null,
  });
  return {
    side: t.side,
    entryCents: est?.avgCents ?? (t.type === "limit" ? limit : null) ?? est?.ask ?? null,
    qty: est?.qty ?? 0,
    bid: est?.bid ?? null,
    tpCents: tp,
    slCents: sl,
  };
}

/** The book is polled every second; older than this it no longer counts as a quote. */
export const BOOK_FRESH_MS = 5_000;

/**
 * Fresh best bid per side for the current window: the odds feed (<= STALE_AFTER_MS old) first, else the
 * polled book (<= BOOK_FRESH_MS old). Null = no usable quote.
 */
export function quoteBids(st: TerminalState, now: number): Record<Side, number | null> {
  const out: Record<Side, number | null> = { up: null, down: null };
  const cur = st.current;
  if (!cur) return out;
  const o = selectOdds(st);
  if (o && now - o.ts <= STALE_AFTER_MS) {
    out.up = o.upBid;
    out.down = o.downBid;
  }
  const b = st.book;
  if ((out.up == null || out.down == null) && b && b.windowId === cur.id && now - b.ts <= BOOK_FRESH_MS) {
    out.up ??= sideBook(b, "up").bids[0]?.price ?? null;
    out.down ??= sideBook(b, "down").bids[0]?.price ?? null;
  }
  return out;
}

export function linesFromState(st: TerminalState, now: number = liveNow()): PositionLine[] {
  if (st.replay.active) return [];
  return buildLines({
    windowId: st.current?.id ?? null,
    positions: Object.values(st.positions),
    brackets: st.brackets,
    drafts: st.bracketDrafts,
    fills: st.fills,
    ticket: ticketPreview(st),
    bids: quoteBids(st, now),
  });
}

const changed = (s: TerminalState, p: TerminalState) =>
  s.positions !== p.positions ||
  s.brackets !== p.brackets ||
  s.bracketDrafts !== p.bracketDrafts ||
  s.fills !== p.fills ||
  s.ticket !== p.ticket ||
  s.current !== p.current ||
  s.replay.active !== p.replay.active ||
  s.book !== p.book ||
  s.lastOdds !== p.lastOdds;

/** re-evaluate quote freshness this often while lines are shown (a silent feed changes no store state) */
const FRESHNESS_CHECK_MS = 1000;

export class PositionLinesController {
  private lines: PositionLine[] = [];
  private byKey = new Map<string, PositionLine>();
  private drag: DragState | null = null;
  private readonly overlay: LabelOverlay;
  private readonly sync: BracketSync;
  private readonly unsub: () => void;
  private readonly freshTimer: ReturnType<typeof setInterval>;
  /** liveNow() when the PUT now in flight for a position was sent (one at a time per position) */
  private readonly sentAt = new Map<string, number>();

  constructor(
    private readonly h: ChartHandle,
    private readonly prim: PositionLinesPrimitive,
    put: (positionId: string, b: BracketInput | null) => Promise<Bracket | null> = api.putBracket,
  ) {
    this.sync = new BracketSync({
      put: (id, b) => {
        this.sentAt.set(id, liveNow());
        return put(id, b);
      },
      getDraft: (id) => useTerminal.getState().bracketDrafts[id],
      getServer: (id) => bracketOf(id, useTerminal.getState().brackets, {}),
      clearDraft: (id, sent) => {
        const st = useTerminal.getState();
        if (st.bracketDrafts[id] === sent) st.setBracketDraft(id, null);
      },
      onSaved: (id, saved) => this.onSaved(id, saved),
      onError: (id, err) => this.onError(id, err),
    });
    this.overlay = new LabelOverlay(h.container, {
      dragStart: (k) => this.dragStart(k),
      dragMove: (k, y) => this.dragMove(k, y),
      dragEnd: (k, commit) => this.dragEnd(k, commit),
      nudge: (k, steps) => this.nudge(k, steps),
      remove: (k) => this.remove(k),
      add: (pid, kind) => this.add(pid, kind),
      hover: (k) => {
        this.prim.hoverKey = k;
        this.prim.update();
      },
    });
    this.overlay.onFontsReady = () => this.prim.update();
    prim.overlay = this.overlay;
    prim.getLines = () => this.lines;
    this.rebuild();
    this.unsub = useTerminal.subscribe((s, p) => {
      if (changed(s, p)) this.rebuild();
    });
    this.freshTimer = setInterval(() => {
      if (this.lines.length) this.rebuild();
    }, FRESHNESS_CHECK_MS);
  }

  destroy() {
    this.unsub();
    clearInterval(this.freshTimer);
    // a drag cut short by a remount is a cancelled gesture: back to where it started
    if (this.drag) this.dragEnd(this.drag.key, false);
    this.overlay.destroy();
    this.prim.overlay = null;
    this.prim.getLines = () => [];
    // pending writes go out now; in-flight ones still report and drop their drafts
    this.sync.dispose();
  }

  /** current lines (tests / debugging) */
  current(): readonly PositionLine[] {
    return this.lines;
  }

  private rebuild() {
    const st = useTerminal.getState();
    // a position that left 'open' (TP/SL fired, closed, settled): its queued writes are moot
    for (const id of this.sync.ids()) {
      if (st.positions[id]?.status === "open") continue;
      this.sync.forget(id);
      if (st.bracketDrafts[id]) st.setBracketDraft(id, null);
    }
    this.lines = linesFromState(useTerminal.getState());
    this.byKey = new Map(this.lines.map((l) => [l.key, l]));
    // the dragged line vanished (position closed / settled, replay started, ticket bracket switched off)
    if (this.drag && !this.byKey.has(this.drag.key)) this.abortDrag();
    this.prim.update();
  }

  // ---- drag -------------------------------------------------------------------------------------------

  private dragStart(key: string): boolean {
    this.rebuild(); // bounds against the quote as of now
    const line = this.byKey.get(key);
    if (!line || line.kind === "entry" || !line.bounds) return false;
    const st = useTerminal.getState();
    if (line.draft) {
      this.drag = {
        key,
        draft: true,
        positionId: null,
        kind: line.kind,
        orig: null,
        origText: line.kind === "tp" ? st.ticket.tpCents : st.ticket.slCents,
        moved: false,
      };
    } else {
      const pid = line.positionId as string;
      this.drag = {
        key,
        draft: false,
        positionId: pid,
        kind: line.kind,
        orig: bracketInput(bracketOf(pid, st.brackets, st.bracketDrafts)),
        origText: "",
        moved: false,
      };
      this.sync.hold(pid);
    }
    this.prim.activeKey = key;
    this.prim.update();
    return true;
  }

  private dragMove(key: string, y: number) {
    const d = this.drag;
    const line = this.byKey.get(key);
    if (!d || d.key !== key || !line) return;
    const cents = snapCents(this.h.yToCents(y), line.bounds);
    if (cents == null || cents === line.cents) return;
    d.moved = true;
    this.setLeg(line, cents, false);
  }

  private dragEnd(key: string, commit: boolean) {
    const d = this.drag;
    if (!d || d.key !== key) return;
    this.drag = null;
    this.prim.activeKey = null;
    if (!commit && d.moved) {
      if (d.draft) useTerminal.getState().setTicket(d.kind === "tp" ? { tpCents: d.origText } : { slCents: d.origText });
      else if (d.positionId && d.orig) useTerminal.getState().setBracketDraft(d.positionId, d.orig);
    }
    if (!d.draft && d.positionId) this.sync.release(d.positionId);
    this.prim.update();
  }

  private abortDrag() {
    const d = this.drag;
    if (!d) return;
    this.drag = null;
    this.prim.activeKey = null;
    if (!d.draft && d.positionId) {
      const st = useTerminal.getState();
      const pos = st.positions[d.positionId];
      if (!pos || pos.status !== "open") {
        // the position is gone: nothing left to protect
        this.sync.forget(d.positionId);
        if (st.bracketDrafts[d.positionId]) st.setBracketDraft(d.positionId, null);
      } else this.sync.release(d.positionId);
    }
  }

  // ---- keyboard / buttons ------------------------------------------------------------------------------

  private nudge(key: string, steps: number) {
    this.rebuild();
    const line = this.byKey.get(key);
    if (!line || line.kind === "entry") return;
    const cents = nudgeCents(line.cents, steps, line.bounds);
    if (cents == null || cents === line.cents) return;
    this.setLeg(line, cents, false);
  }

  private remove(key: string) {
    const line = this.byKey.get(key);
    if (!line || line.kind === "entry") return;
    if (this.drag?.key === key) this.dragEnd(key, true);
    if (line.draft) {
      useTerminal.getState().setTicket(line.kind === "tp" ? { tpOn: false } : { slOn: false });
      return;
    }
    this.setLeg(line, null, true);
  }

  private add(positionId: string, kind: LegKind) {
    const st = useTerminal.getState();
    const pos = st.positions[positionId];
    if (!pos || pos.status !== "open" || exitingBracket(positionId, st.brackets)) return;
    const base = bracketOf(positionId, st.brackets, st.bracketDrafts);
    if ((kind === "tp" ? base?.tpCents : base?.slCents) != null) return;
    // no fresh bid: nothing to place the leg against (it could fire on the first quote)
    const bid = quoteBids(st, liveNow())[pos.side];
    const bounds = positionLegBounds(kind, { tpCents: base?.tpCents ?? null, slCents: base?.slCents ?? null }, bid, pos.markCents);
    if (bid == null || !bounds) return;
    const v = defaultLeg(kind, bid, bounds);
    if (v == null) return;
    st.setBracketDraft(positionId, withLeg(base, kind, v));
    this.sync.flush(positionId);
  }

  /** Write one leg (null = off): ticket text for a preview line, bracket draft + PUT for a position. */
  private setLeg(line: PositionLine, cents: number | null, now: boolean) {
    const st = useTerminal.getState();
    if (line.draft) {
      if (cents == null) return;
      st.setTicket(line.kind === "tp" ? { tpCents: String(cents) } : { slCents: String(cents) });
      return;
    }
    const pid = line.positionId;
    if (!pid || line.kind === "entry") return;
    const base = bracketOf(pid, st.brackets, st.bracketDrafts);
    st.setBracketDraft(pid, withLeg(base, line.kind, cents));
    if (now) this.sync.flush(pid);
    else this.sync.schedule(pid);
  }

  // ---- server results ----------------------------------------------------------------------------------

  private onSaved(positionId: string, saved: Bracket | null) {
    const st = useTerminal.getState();
    const sentAt = this.sentAt.get(positionId) ?? liveNow();
    this.sentAt.delete(positionId);
    const up = savedBracketUpdates(st.brackets, positionId, saved, sentAt, liveNow());
    if (up.length) st.upsertBrackets(up);
  }

  private onError(positionId: string, err: unknown) {
    this.sentAt.delete(positionId);
    const st = useTerminal.getState();
    const gone = isApiError(err) && err.code === "not_found";
    st.pushNotice({
      id: `bracket-${positionId}`,
      ts: liveNow(),
      level: "error",
      title: "Bracket not updated",
      message: gone ? "The position is no longer open." : err instanceof Error ? err.message : String(err),
    });
    // back to the server's bracket (a drag in progress writes a new draft on its next move)
    if (st.bracketDrafts[positionId]) st.setBracketDraft(positionId, null);
  }
}

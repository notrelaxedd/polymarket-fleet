// Store-bound drawing commands shared by the chart's DrawingLayer, its floating toolbar and the tools rail.
// Every edit is recorded as a per-drawing diff on the undo history (./history) and queued on the debounced
// DrawingSync to /api/drawings (./sync), which merges it by id with the server's list.

import type { AlertInput, Drawing } from "@shared/protocol";
import { api } from "@/lib/api";
import { API_URL, isMock } from "@/lib/env";
import { liveMarket } from "@/lib/replay";
import { liveNow, selectOdds, useTerminal } from "@/lib/store";
import { fmtBtc, fmtCents } from "../fmt";
import { alertForLine, alertRefs, duplicateAlert } from "./alerting";
import { DrawingHistory, diffLists } from "./history";
import { replaceDrawing } from "./model";
import { DrawingSync, reqHttp, type SyncStatus } from "./sync";
import { useDrawingUi } from "./ui";

export { alertForLine } from "./alerting";

const SAVE_NOTICE = "drawings-save";
const LOADING_NOTICE = "drawings-loading";

const st = () => useTerminal.getState();
const ui = () => useDrawingUi.getState();

const errText = (e: unknown) => (e instanceof Error && e.message ? e.message : "Request failed");

const history = new DrawingHistory((canUndo, canRedo) => ui().setHistory(canUndo, canRedo));

const remapListeners = new Set<(from: string, to: string) => void>();
/** a drawing created locally got its server id (the controller re-targets an in-progress drag) */
export function onDrawingRemap(fn: (from: string, to: string) => void): () => void {
  remapListeners.add(fn);
  return () => remapListeners.delete(fn);
}

function onSaveStatus(status: SyncStatus, err: unknown) {
  ui().setSave(status, err == null ? null : errText(err));
  const s = st();
  const notice = (level: "warn" | "error", message: string) =>
    s.pushNotice({ id: SAVE_NOTICE, ts: Date.now(), level, title: "Drawings not saved", message });
  if (status === "retrying") notice("warn", `${errText(err)} — retrying. Your drawings stay on the chart.`);
  else if (status === "offline")
    notice("error", `${errText(err)} — gave up retrying. Recent drawing changes are only on this page and are lost on reload.`);
  else if (status === "rejected") notice("error", `${errText(err)} — the change was undone; the chart shows the saved drawings.`);
  else if (status === "saved") s.dismissNotice(SAVE_NOTICE);
}

function onRemap(from: string, to: string) {
  history.remap(from, to);
  const s = st();
  if (s.selectedDrawingId === from) s.selectDrawing(to);
  const ed = ui().editing;
  if (ed?.id === from) ui().setEditing({ id: to, draft: ed.draft ? { ...ed.draft, id: to } : null });
  for (const f of remapListeners) f(from, to);
}

let sync: DrawingSync | null = null;

/** the app-wide save queue (started on first use; loads the server list before anything is saved) */
export function drawingSync(): DrawingSync {
  if (sync) return sync;
  sync = new DrawingSync(
    {
      list: () => api.drawings(),
      createDrawing: (d) => api.createDrawing(d),
      replaceDrawings: (l) => api.replaceDrawings(l),
      updateDrawing: (id, p) => api.updateDrawing(id, p),
      deleteDrawing: (id) => api.deleteDrawing(id),
      deleteAllDrawings: () => api.deleteAllDrawings(),
    },
    {
      get: () => st().drawings,
      set: (l) => st().setDrawings(l),
      subscribe: (fn) => useTerminal.subscribe((s, p) => s.drawings !== p.drawings && fn(s.drawings)),
    },
    { onStatus: onSaveStatus, onRemap },
  ).start();
  if (typeof window !== "undefined") {
    window.addEventListener("pagehide", flushOnUnload);
    // a plain reload is covered by the keepalive flush; only warn when saving is failing
    window.addEventListener("beforeunload", (e) => {
      const s = sync?.status();
      if (s === "retrying" || s === "offline") {
        e.preventDefault();
        e.returnValue = "";
      }
    });
  }
  return sync;
}

/**
 * The page is going away: send what is still unsaved with keepalive requests (a normal fetch is cancelled
 * on unload). The mock backend lives in this page's memory, so there is nothing to send to.
 */
function flushOnUnload() {
  if (!sync) return;
  if (isMock()) {
    void sync.flushNow();
    return;
  }
  for (const r of sync.takeUnloadRequests()) {
    const h = reqHttp(r);
    try {
      void fetch(`${API_URL}/api${h.path}`, {
        method: h.method,
        keepalive: true,
        headers: h.body === undefined ? undefined : { "content-type": "application/json" },
        body: h.body === undefined ? undefined : JSON.stringify(h.body),
      }).catch(() => {});
    } catch {
      // keepalive quota exceeded / fetch unavailable: nothing more can be done while unloading
    }
  }
}

/** false (with a notice) until the server's drawing list has loaded */
export function drawingsReady(): boolean {
  if (drawingSync().ready()) return true;
  st().pushNotice({
    id: LOADING_NOTICE,
    ts: Date.now(),
    level: "info",
    title: "Drawings are loading",
    message: "Your saved drawings are still loading — try again in a moment.",
  });
  return false;
}

function dropStaleSelection() {
  const s = st();
  if (s.selectedDrawingId && !s.drawings.some((d) => d.id === s.selectedDrawingId)) s.selectDrawing(null);
}

/** replace the whole list as one undoable step (recorded as the per-drawing changes). False when refused. */
export function commitDrawings(next: Drawing[]): boolean {
  if (!drawingsReady()) return false;
  const step = diffLists(st().drawings, next);
  if (!step.length) return false;
  drawingSync().local(() => st().setDrawings(next));
  history.record(step);
  dropStaleSelection();
  return true;
}

export function addDrawing(d: Drawing, select = true): boolean {
  if (!commitDrawings([...st().drawings, d])) return false;
  if (select) st().selectDrawing(d.id);
  return true;
}

/** apply `fn` to one drawing as one undoable step (no-op when unchanged or missing) */
export function updateDrawing(id: string, fn: (d: Drawing) => Drawing) {
  const list = st().drawings;
  const cur = list.find((d) => d.id === id);
  if (!cur) return;
  const next = fn(cur);
  if (next === cur) return;
  commitDrawings(replaceDrawing(list, next));
}

export function deleteDrawing(id: string) {
  const list = st().drawings;
  if (!list.some((d) => d.id === id)) return;
  commitDrawings(list.filter((d) => d.id !== id));
}

/** Del key / toolbar: delete the selection unless it is locked. Returns true when something was deleted. */
export function deleteSelected(): boolean {
  const s = st();
  const d = s.drawings.find((x) => x.id === s.selectedDrawingId);
  if (!d || d.locked || s.drawingsLocked) return false;
  deleteDrawing(d.id);
  return true;
}

export function clearAllDrawings() {
  if (!st().drawings.length) return;
  commitDrawings([]);
}

export function canUndoDrawings(): boolean {
  return history.canUndo();
}

export function canRedoDrawings(): boolean {
  return history.canRedo();
}

/** undo this client's newest drawing edit (other clients' drawings are left alone) */
export function undoDrawings(): boolean {
  if (!history.canUndo() || !drawingsReady()) return false;
  const next = history.undo(st().drawings);
  if (!next) return false;
  drawingSync().local(() => st().setDrawings(next));
  dropStaleSelection();
  return true;
}

export function redoDrawings(): boolean {
  if (!history.canRedo() || !drawingsReady()) return false;
  const next = history.redo(st().drawings);
  if (!next) return false;
  drawingSync().local(() => st().setDrawings(next));
  dropStaleSelection();
  return true;
}

/** retry a failed / given-up save now */
export function retryDrawingSave() {
  void drawingSync().retry();
}

export function dismissDrawingSaveError() {
  drawingSync().dismissError();
  st().dismissNotice(SAVE_NOTICE);
}

export function selectedDrawing(): Drawing | null {
  const s = st();
  return s.drawings.find((d) => d.id === s.selectedDrawingId) ?? null;
}

function alertText(a: AlertInput): string {
  const t = a.threshold ?? 0;
  switch (a.kind) {
    case "btc_above":
      return `BTC above ${fmtBtc(t)}`;
    case "btc_below":
      return `BTC below ${fmtBtc(t)}`;
    case "up_above":
      return `Up above ${fmtCents(t)}`;
    case "up_below":
      return `Up below ${fmtCents(t)}`;
    default:
      return "Alert";
  }
}

/**
 * Tools-rail / toolbar "Add alert": with a horizontal line selected, create a one-shot price alert at it;
 * otherwise open the New alert dialog. Without a fresh current value (stale feed, replay, previous window)
 * or with the line right at the current value the direction is not guessed: the dialog opens prefilled.
 * One request at a time; an identical enabled alert is not created twice.
 */
export async function addAlertFromSelection(drawing: Drawing | null = selectedDrawing()): Promise<void> {
  const s = st();
  if (!drawing || drawing.kind !== "hline") {
    s.openNewAlert(null);
    return;
  }
  if (ui().alertBusy) return;
  const p = drawing.points[0];
  // the live market even during a replay (the line's price is absolute; the alert watches the real feed)
  const lm = liveMarket();
  const refs = alertRefs({ connected: s.connected, replaying: false, lastTick: lm.lastTick, odds: selectOdds(lm) }, liveNow());
  const input = alertForLine(drawing, refs.btc, refs.upMid);
  if (!input) {
    s.openNewAlert({ kind: p.scale === "odds" ? "up_above" : "btc_above", threshold: p.price, note: drawing.label?.trim() || null });
    return;
  }
  if (duplicateAlert(s.alerts, input)) {
    s.pushNotice({
      id: `alert-line-dup-${drawing.id}`,
      ts: Date.now(),
      level: "info",
      title: "Alert already set",
      message: `${alertText(input)} is already armed.`,
    });
    return;
  }
  ui().setAlertBusy(true);
  try {
    await api.createAlert(input);
    st().pushNotice({
      id: `alert-line-${drawing.id}-${Date.now()}`,
      ts: Date.now(),
      level: "success",
      title: "Alert created",
      message: `${alertText(input)} · fires once · stays at this price if the line is moved`,
    });
  } catch (e) {
    st().pushNotice({
      id: `alert-line-err-${drawing.id}`,
      ts: Date.now(),
      level: "error",
      title: "Could not create alert",
      message: errText(e),
    });
    st().openNewAlert(input);
  } finally {
    ui().setAlertBusy(false);
  }
}

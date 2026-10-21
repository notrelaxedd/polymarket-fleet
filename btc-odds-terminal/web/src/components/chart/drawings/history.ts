// Undo / redo of drawing edits as per-drawing diffs (no DOM, no store).
//
// A step records, for every drawing it touched, the version before and after (null = absent). Undo and redo
// apply that diff by id onto whatever the list is *now*, so drawings this client never touched — added,
// edited or deleted by another tab, or by a server snapshot after a reconnect — are left alone. A change to
// a drawing that has since disappeared is skipped instead of resurrecting it.

import type { Drawing } from "@shared/protocol";
import { sameInput } from "./model";

export interface Change {
  id: string;
  before: Drawing | null;
  after: Drawing | null;
  /** list position before / after the step (where an undo / redo re-inserts the drawing) */
  beforeIndex: number;
  afterIndex: number;
}

export type Step = Change[];

export const HISTORY_CAP = 100;

/** the per-drawing changes that turn `before` into `after` (content changes only; timestamps ignored) */
export function diffLists(before: readonly Drawing[], after: readonly Drawing[]): Step {
  const b = new Map(before.map((d, i) => [d.id, { d, i }]));
  const a = new Map(after.map((d, i) => [d.id, { d, i }]));
  const out: Step = [];
  for (const [id, x] of b) {
    const y = a.get(id);
    if (!y) out.push({ id, before: x.d, after: null, beforeIndex: x.i, afterIndex: -1 });
    else if (y.d !== x.d && !sameInput(x.d, y.d)) out.push({ id, before: x.d, after: y.d, beforeIndex: x.i, afterIndex: y.i });
  }
  for (const [id, y] of a) if (!b.has(id)) out.push({ id, before: null, after: y.d, beforeIndex: -1, afterIndex: y.i });
  return out;
}

/**
 * Apply one step to `list`: "undo" restores every `before`, "redo" every `after`. A drawing the step left
 * in place but which is gone now (deleted elsewhere) is not brought back.
 */
export function applyStep(list: readonly Drawing[], step: Step, dir: "undo" | "redo"): Drawing[] {
  const out = [...list];
  const changes = dir === "undo" ? [...step].reverse() : step;
  for (const c of changes) {
    const target = dir === "undo" ? c.before : c.after;
    const left = dir === "undo" ? c.after : c.before;
    const index = dir === "undo" ? c.beforeIndex : c.afterIndex;
    const i = out.findIndex((d) => d.id === c.id);
    if (!target) {
      if (i >= 0) out.splice(i, 1);
    } else if (i >= 0) {
      out[i] = target;
    } else if (!left) {
      out.splice(Math.max(0, Math.min(out.length, index)), 0, target);
    }
  }
  return out;
}

const renameIn = (d: Drawing | null, from: string, to: string): Drawing | null => (d && d.id === from ? { ...d, id: to } : d);

export class DrawingHistory {
  private undoStack: Step[] = [];
  private redoStack: Step[] = [];

  constructor(private readonly onChange?: (canUndo: boolean, canRedo: boolean) => void) {}

  canUndo(): boolean {
    return this.undoStack.length > 0;
  }
  canRedo(): boolean {
    return this.redoStack.length > 0;
  }

  /** a new local edit: pushes it and drops the redo stack (no-op for an empty step) */
  record(step: Step) {
    if (!step.length) return;
    this.undoStack = [...this.undoStack, step].slice(-HISTORY_CAP);
    this.redoStack = [];
    this.changed();
  }

  /** the list after undoing the newest step (null when there is nothing to undo) */
  undo(list: readonly Drawing[]): Drawing[] | null {
    const step = this.undoStack[this.undoStack.length - 1];
    if (!step) return null;
    this.undoStack = this.undoStack.slice(0, -1);
    this.redoStack = [...this.redoStack, step].slice(-HISTORY_CAP);
    this.changed();
    return applyStep(list, step, "undo");
  }

  redo(list: readonly Drawing[]): Drawing[] | null {
    const step = this.redoStack[this.redoStack.length - 1];
    if (!step) return null;
    this.redoStack = this.redoStack.slice(0, -1);
    this.undoStack = [...this.undoStack, step].slice(-HISTORY_CAP);
    this.changed();
    return applyStep(list, step, "redo");
  }

  /** the server gave a drawing a new id (POST): keep the history pointing at it */
  remap(from: string, to: string) {
    const fix = (s: Step): Step =>
      s.some((c) => c.id === from)
        ? s.map((c) => (c.id === from ? { ...c, id: to, before: renameIn(c.before, from, to), after: renameIn(c.after, from, to) } : c))
        : s;
    this.undoStack = this.undoStack.map(fix);
    this.redoStack = this.redoStack.map(fix);
  }

  clear() {
    this.undoStack = [];
    this.redoStack = [];
    this.changed();
  }

  private changed() {
    this.onChange?.(this.canUndo(), this.canRedo());
  }
}

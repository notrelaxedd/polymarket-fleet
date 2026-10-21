// Local UI state of the drawings section (not part of the app store).

import { create } from "zustand";
import type { Drawing } from "@shared/protocol";
import type { SyncStatus } from "./sync";

export interface DrawingEditing {
  id: string;
  /** a new text note that is not in the store yet (added on commit, discarded on cancel / empty text) */
  draft: Drawing | null;
}

interface DrawingUiState {
  /** "Delete all drawings?" confirm dialog (tools rail) */
  confirmClear: boolean;
  /** text note / horizontal-line label being edited in the inline editor */
  editing: DrawingEditing | null;
  /** save queue state (see DrawingSync) + the last error's message */
  saveStatus: SyncStatus;
  saveError: string | null;
  /** diff-based undo / redo stacks (actions.ts) */
  canUndo: boolean;
  canRedo: boolean;
  /** an "alert at this line" request is in flight */
  alertBusy: boolean;
  setConfirmClear: (open: boolean) => void;
  setEditing: (e: DrawingEditing | null) => void;
  setSave: (status: SyncStatus, error: string | null) => void;
  setHistory: (canUndo: boolean, canRedo: boolean) => void;
  setAlertBusy: (busy: boolean) => void;
}

export const useDrawingUi = create<DrawingUiState>()((set) => ({
  confirmClear: false,
  editing: null,
  saveStatus: "loading",
  saveError: null,
  canUndo: false,
  canRedo: false,
  alertBusy: false,
  setConfirmClear: (confirmClear) => set({ confirmClear }),
  setEditing: (editing) => set({ editing }),
  setSave: (saveStatus, saveError) => set({ saveStatus, saveError }),
  setHistory: (canUndo, canRedo) => set({ canUndo, canRedo }),
  setAlertBusy: (alertBusy) => set({ alertBusy }),
}));

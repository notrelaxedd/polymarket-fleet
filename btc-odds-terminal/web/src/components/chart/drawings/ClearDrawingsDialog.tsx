"use client";
// "Delete all drawings?" confirmation, opened from the tools rail (useDrawingUi.confirmClear).

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { useTerminal } from "@/lib/store";
import { clearAllDrawings } from "./actions";
import { useDrawingUi } from "./ui";

export function ClearDrawingsDialog() {
  const open = useDrawingUi((s) => s.confirmClear);
  const setOpen = useDrawingUi((s) => s.setConfirmClear);
  const count = useTerminal((s) => s.drawings.length);
  const n = `${count} ${count === 1 ? "drawing" : "drawings"}`;
  return (
    <Dialog
      open={open}
      onOpenChange={setOpen}
      tone="danger"
      title="Delete all drawings?"
      description={`Removes ${n} and stickers from the chart, including locked ones. Undo (Ctrl/Cmd+Z) brings them back.`}
      footer={
        <>
          <Button variant="outline" onClick={() => setOpen(false)} data-autofocus="">
            Cancel
          </Button>
          <Button
            variant="danger"
            onClick={() => {
              clearAllDrawings();
              setOpen(false);
            }}
          >
            Delete all
          </Button>
        </>
      }
    />
  );
}

/** rail button handler: confirm when there is something to delete */
export function requestClearDrawings() {
  if (useTerminal.getState().drawings.length) useDrawingUi.getState().setConfirmClear(true);
}

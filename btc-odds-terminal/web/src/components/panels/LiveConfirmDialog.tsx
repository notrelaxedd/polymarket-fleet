"use client";
// First-live-order-of-the-session confirm. OWNER: trading-ui.
// `requestLiveConfirm()` opens it and resolves true once api.liveConfirm() returned a session (stored via
// setLiveSession -> store.liveSession / liveConfirmedThisSession), false on cancel. panels/actions.ts calls it
// before every live order / close and sends liveSession in the request.
import * as React from "react";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { useTerminal } from "@/lib/store";
import { errorText } from "./actions";

let pending: ((ok: boolean) => void) | null = null;
let openSetter: ((open: boolean) => void) | null = null;

/** Resolves true when live trading is (or becomes) confirmed for this browser session. */
export function requestLiveConfirm(): Promise<boolean> {
  if (useTerminal.getState().liveConfirmedThisSession) return Promise.resolve(true);
  if (!openSetter) return Promise.resolve(false);
  pending?.(false);
  return new Promise<boolean>((resolve) => {
    pending = resolve;
    openSetter?.(true);
  });
}

export function LiveConfirmDialog() {
  const [open, setOpen] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const setLiveSession = useTerminal((s) => s.setLiveSession);

  React.useEffect(() => {
    openSetter = setOpen;
    return () => {
      openSetter = null;
    };
  }, []);

  const finish = React.useCallback((ok: boolean) => {
    setOpen(false);
    setError(null);
    pending?.(ok);
    pending = null;
  }, []);
  const onOpenChange = React.useCallback((o: boolean) => !o && finish(false), [finish]);

  const confirm = async () => {
    setBusy(true);
    try {
      const r = await api.liveConfirm();
      setLiveSession(r.session);
      finish(true);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      tone="danger"
      title="Place a LIVE order?"
      description="Live mode sends real orders to Polymarket US with real money. This confirmation lasts for this browser session."
      footer={
        <>
          <Button variant="outline" onClick={() => finish(false)} data-autofocus>
            Cancel
          </Button>
          <Button variant="down" onClick={confirm} disabled={busy}>
            {busy ? "Confirming…" : "Confirm live trading"}
          </Button>
        </>
      }
    >
      <ul className="list-disc space-y-1 pl-4 text-[12px] leading-4 text-text-2">
        <li>Orders go to the exchange at once; fills, fees and slippage are real. Risk limits still apply.</li>
        <li>Brackets are enforced by the worker, not the exchange: keep it running.</li>
        <li>Switch back to Paper in Settings at any time.</li>
      </ul>
      {error && (
        <p role="alert" className="mt-2 text-[12px] text-down">
          {error}
        </p>
      )}
    </Dialog>
  );
}

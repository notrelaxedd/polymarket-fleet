"use client";
// Edit bracket for an open position. OWNER: trading-ui. While the form is open each valid change is mirrored to
// store.bracketDrafts[positionId] so the chart's TP/SL lines follow; Save PUTs the bracket, Cancel drops the draft.
import * as React from "react";
import type { Bracket, BracketInput, Position } from "@shared/protocol";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { useLiveMarket } from "@/lib/replay";
import { useTerminal } from "@/lib/store";
import { quoteCents } from "@/lib/format";
import { BracketRows } from "./BracketRows";
import { errorText } from "./actions";
import { bracketFromFields, fieldsFromBracket, validateBracket, type BracketFields } from "./ticket-math";

export function EditBracketDialog({
  open,
  onClose,
  position,
  bracket,
  markCents,
  secLeft,
}: {
  open: boolean;
  onClose: () => void;
  position: Position;
  /** the stored active bracket (null = none yet) */
  bracket: Bracket | null;
  markCents: number | null;
  secLeft: number | null;
}) {
  // live BTC even during a replay: the watcher compares the real tick
  const btcPrice = useLiveMarket((m) => m.lastTick?.price ?? null);
  const setBracketDraft = useTerminal((s) => s.setBracketDraft);
  const upsertBrackets = useTerminal((s) => s.upsertBrackets);
  const [fields, setFields] = React.useState<BracketFields>(() => fieldsFromBracket(bracket));
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  // reload from the stored bracket each time it opens
  const wasOpen = React.useRef(false);
  React.useEffect(() => {
    if (open && !wasOpen.current) {
      setFields(fieldsFromBracket(bracket));
      setError(null);
    }
    wasOpen.current = open;
  }, [open, bracket]);

  const errors = validateBracket(fields, position.side, { entry: null, bid: markCents, btc: btcPrice, secLeft });
  const valid = Object.keys(errors).length === 0;
  const slip = bracket?.maxSlippageCents ?? null;

  // mirror valid edits to the chart (draft wins over the stored bracket in selectBracketFor)
  React.useEffect(() => {
    if (!open) return;
    if (valid) {
      const b = bracketFromFields(fields, slip);
      setBracketDraft(position.id, b ?? emptyBracket(slip));
    }
  }, [open, valid, fields, slip, position.id, setBracketDraft]);

  const close = React.useCallback(() => {
    setBracketDraft(position.id, null);
    onClose();
  }, [setBracketDraft, position.id, onClose]);
  const onOpenChange = React.useCallback((o: boolean) => !o && close(), [close]);

  const save = async (e?: React.FormEvent) => {
    e?.preventDefault();
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    try {
      const next = bracketFromFields(fields, slip);
      const r = await api.putBracket(position.id, next);
      if (r) upsertBrackets([r]);
      else if (bracket) upsertBrackets([{ ...bracket, status: "cancelled", updatedTs: Date.now() }]);
      close();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const side = position.side === "up" ? "Up" : "Down";
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Edit bracket · ${side} ${position.qty.toFixed(1)} @ ${quoteCents(position.avgCents)}`}
      description="One cancels the other: the first leg to trigger exits the whole position with a marketable order capped by max slippage."
      footer={
        <>
          {bracket && (
            <Button variant="danger" className="mr-auto" disabled={busy} onClick={() => setFields(fieldsFromBracket(null))}>
              Clear all legs
            </Button>
          )}
          <Button variant="outline" onClick={close}>
            Cancel
          </Button>
          <Button variant="up" type="submit" form={`eb-${position.id}`} disabled={!valid || busy}>
            {busy ? "Saving…" : "Save bracket"}
          </Button>
        </>
      }
    >
      <form id={`eb-${position.id}`} onSubmit={save} noValidate>
        <BracketRows idPrefix={`eb-${position.id}`} side={position.side} value={fields} onChange={(p) => setFields((f) => ({ ...f, ...p }))} errors={errors} />
        <p className="pt-2 text-[12px] text-muted">
          Mark <span className="num text-text-2">{quoteCents(markCents)}</span> · TP and SL compare the {side} bid. Turning every leg off removes
          the bracket.
        </p>
        {error && (
          <p role="alert" className="pt-2 text-[12px] text-down">
            {error}
          </p>
        )}
      </form>
    </Dialog>
  );
}

const emptyBracket = (slip: number | null): BracketInput => ({ tpCents: null, slCents: null, btcStop: null, timeExitSec: null, maxSlippageCents: slip });

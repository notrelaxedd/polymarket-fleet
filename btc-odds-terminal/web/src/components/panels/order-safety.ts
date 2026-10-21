// Order-safety helpers for the trading cards (pure; unit-tested in __tests__/order-safety.test.ts). OWNER: trading-ui.
//   - OrderKey: the clientOrderId (idempotency key) of the current ticket draft. A retry after an ambiguous
//     failure (timeout, network error, 5xx) reuses it, so the backend returns the order it already placed
//     instead of buying twice; it rotates after a definitive answer (success, 4xx) or when the draft changes.
//   - newerOnly: drop REST records that are older than what the WebSocket already delivered.
//   - stillOpenText: the kill switch never reports a clean flatten while positions remain.
import { isApiError } from "@/lib/api-error";

export class OrderKey {
  private key: string | null = null;
  private sig: string | null = null;
  constructor(private readonly gen: () => string) {}

  /** Key for the draft `sig` (any stable serialization of what the user entered). A new draft gets a new key. */
  for(sig: string): string {
    if (this.key == null || sig !== this.sig) {
      this.key = this.gen();
      this.sig = sig;
    }
    return this.key;
  }

  /** The current key (null when none is pending). */
  get pending(): string | null {
    return this.key;
  }

  /** After a definitive result: the next submit is a new order. */
  rotate(): void {
    this.key = null;
    this.sig = null;
  }
}

/** True when the order may or may not have reached the server (retry with the SAME clientOrderId). */
export function isAmbiguousFailure(e: unknown): boolean {
  if (isApiError(e)) return e.status >= 500;
  if (e instanceof Error && e.name === "CancelledError") return false; // live confirm declined: nothing was sent
  // AbortError (REQUEST_TIMEOUT_MS), TypeError "Failed to fetch" and anything unexpected
  return true;
}

/** `incoming` minus the records whose stored copy is newer (a late REST response must not regress WS state). */
export function newerOnly<T extends { id: string; updatedTs: number }>(cur: Record<string, T>, incoming: T[]): T[] {
  return incoming.filter((x) => {
    const have = cur[x.id];
    return !have || !(have.updatedTs > x.updatedTs);
  });
}

/** Warning after a kill switch that left positions open (null = everything is flat). */
export function stillOpenText(stillOpen: number): string | null {
  if (stillOpen <= 0) return null;
  return `${stillOpen} position${stillOpen === 1 ? " is" : "s are"} still open: close manually, or they settle at expiry.`;
}

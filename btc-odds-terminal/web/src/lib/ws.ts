import type { Bar, ClientMsg, FairSnap, OddsSnap, ServerMsg } from "@shared/protocol";
import { api } from "./api";
import { WS_URL, isMock } from "./env";
import { TICK_CAP_MS, flush, ingest } from "./ingest";
import { getMock } from "./mock";
import { liveNow, useTerminal } from "./store";

const HISTORY_MS = 3 * 3600 * 1000;
const BACKOFF_MIN = 500;
const BACKOFF_MAX = 15000;
/** The server sends `feeds` every second: this long without ANY message means the socket is dead (half-open). */
export const STALL_MS = 5000;
/** A connection must stay up this long after its snapshot before the reconnect backoff resets. */
export const STABLE_MS = 5000;
const HISTORY_RETRY_MIN = 1000;
const HISTORY_RETRY_MAX = 30000;

/** hist (ascending) + live entries at/after `liveFrom`; live wins on overlap. */
function mergeAscending<T>(hist: T[], live: T[], ts: (x: T) => number): T[] {
  if (!live.length) return hist.slice();
  const cut = ts(live[0]);
  const out = hist.filter((x) => ts(x) < cut);
  for (const x of live) out.push(x);
  return out;
}

type HistKey = "bars" | "odds" | "fair";
type HistData = { bars: Bar[]; odds: OddsSnap[]; fair: FairSnap[] };

let historyToken = 0;

/** Abandon any in-flight history load (its socket closed / the backend stopped). */
function cancelHistory(): void {
  historyToken++;
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/**
 * Fetch the last 3h of bars/odds/fair and install them with NEW arrays (merging anything streamed since
 * `connectedAt`, a SERVER timestamp). Series that fail are retried with backoff until they load or the
 * load is cancelled (socket closed / newer load started).
 */
async function loadHistory(connectedAt: number): Promise<void> {
  const token = ++historyToken;
  // a replay owns the chart's series; its exit installs fresh live history itself (lib/replay.ts)
  if (useTerminal.getState().replay.active) return;
  const pending = new Set<HistKey>(["bars", "odds", "fair"]);
  const fetchers: { [K in HistKey]: (from: number, to: number) => Promise<HistData[K]> } = {
    bars: api.bars,
    odds: api.odds,
    fair: api.fair,
  };
  let first = true;
  let delay = HISTORY_RETRY_MIN;
  for (;;) {
    const to = liveNow() + 60_000;
    const from = to - HISTORY_MS - 60_000;
    const keys = [...pending];
    const res = await Promise.all(keys.map((k) => fetchers[k](from, to).catch(() => null)));
    if (token !== historyToken || useTerminal.getState().replay.active) return;
    flush(); // make sure queued live messages are in the arrays before we merge
    const since = connectedAt - 1000;
    const live = useTerminal.getState().series;
    const patch: Partial<typeof live> = {};
    keys.forEach((k, i) => {
      const got = res[i];
      if (!got) return;
      pending.delete(k);
      if (k === "bars") patch.bars = mergeAscending(got as Bar[], live.bars.filter((b) => b.t >= since), (b) => b.t);
      else if (k === "odds") patch.odds = mergeAscending(got as OddsSnap[], live.odds.filter((o) => o.ts >= since), (o) => o.ts);
      else patch.fair = mergeAscending(got as FairSnap[], live.fair.filter((f) => f.ts >= since), (f) => f.ts);
    });
    if (first && isMock()) {
      const t = getMock().ticks(Math.max(from, to - TICK_CAP_MS), to);
      patch.ticks = mergeAscending(t, live.ticks.filter((x) => x.ts >= since), (x) => x.ts);
    }
    first = false;
    if (Object.keys(patch).length) useTerminal.getState().setHistory(patch);
    if (pending.size === 0) return;
    await sleep(delay);
    if (token !== historyToken) return;
    delay = Math.min(HISTORY_RETRY_MAX, delay * 2);
  }
}

function startMock(): () => void {
  const mock = getMock();
  const st = useTerminal;
  st.getState().applySnapshot(mock.snapshot());
  st.setState({ connected: true });
  void loadHistory(liveNow());
  const off = mock.subscribe((m: ServerMsg) => ingest(m));
  return () => {
    off();
    cancelHistory();
    st.setState({ connected: false });
  };
}

export function startReal(): () => void {
  let attempt = 0;
  let stopped = false;
  let retry: ReturnType<typeof setTimeout> | null = null;
  let teardownCurrent: (() => void) | null = null;

  const scheduleReconnect = () => {
    if (stopped) return;
    const base = Math.min(BACKOFF_MAX, BACKOFF_MIN * 2 ** attempt);
    const delay = Math.round(base * (0.85 + Math.random() * 0.3));
    attempt++;
    retry = setTimeout(connect, delay);
  };

  const connect = () => {
    if (stopped) return;
    let sock: WebSocket;
    try {
      sock = new WebSocket(WS_URL);
    } catch {
      scheduleReconnect();
      return;
    }
    let lastMsgAt = Date.now();
    let gotSnapshot = false;
    let closed = false;
    let ping: ReturnType<typeof setInterval> | null = null;
    let stable: ReturnType<typeof setTimeout> | null = null;

    // Liveness watchdog: a half-open socket (sleep, NAT timeout, wifi handoff) can stay OPEN for minutes
    // with nothing arriving. The server's heartbeat is a protocol ping the browser answers silently, so
    // judge liveness by application messages (`feeds` arrives every second).
    const watchdog = setInterval(() => {
      if (Date.now() - lastMsgAt > STALL_MS) teardown();
    }, 1000);

    /** Idempotent: drop this socket and schedule a reconnect. Does not wait for a close handshake
     *  (on a dead link `onclose` can take minutes). */
    const teardown = () => {
      if (closed) return;
      closed = true;
      clearInterval(watchdog);
      if (ping) clearInterval(ping);
      if (stable) clearTimeout(stable);
      sock.onopen = sock.onmessage = sock.onclose = sock.onerror = null;
      try {
        sock.close();
      } catch {}
      if (teardownCurrent === teardown) teardownCurrent = null;
      cancelHistory();
      useTerminal.setState({ connected: false });
      scheduleReconnect();
    };

    sock.onopen = () => {
      lastMsgAt = Date.now();
      ping = setInterval(() => {
        if (sock.readyState === WebSocket.OPEN) {
          const m: ClientMsg = { type: "ping", ts: Date.now() };
          sock.send(JSON.stringify(m));
        }
      }, 15000);
    };
    sock.onmessage = (ev) => {
      lastMsgAt = Date.now();
      let msg: ServerMsg;
      try {
        msg = JSON.parse(ev.data as string) as ServerMsg;
      } catch {
        return;
      }
      ingest(msg);
      if (msg.type === "snapshot" && !gotSnapshot) {
        // Only now is the session usable: the snapshot sets the server clock offset, so the history range
        // and the live/history `since` cutoff are both in server time.
        gotSnapshot = true;
        flush();
        useTerminal.setState({ connected: true });
        void loadHistory(msg.data.serverTime);
        // reset the backoff only once the connection proved stable (a server that accepts then closes
        // must not cause a fixed-rate reconnect loop)
        stable = setTimeout(() => (attempt = 0), STABLE_MS);
      }
    };
    sock.onclose = teardown;
    sock.onerror = teardown;
    teardownCurrent = teardown;
  };

  connect();
  return () => {
    stopped = true;
    if (retry) clearTimeout(retry);
    teardownCurrent?.(); // no reconnect: stopped
    cancelHistory();
    useTerminal.setState({ connected: false });
  };
}

/**
 * Bring market state back to live after a replay: a fresh snapshot + 3h history (mock or REST).
 * Call AFTER setting replay.active = false (lib/replay.ts does).
 */
export async function resyncLive(): Promise<void> {
  try {
    const snap = await api.state();
    useTerminal.getState().applySnapshot(snap);
  } catch {
    // the socket's next snapshot (reconnect) heals it
  }
  await loadHistory(liveNow());
}

/** Start the data backend (WebSocket or mock). Returns a stop function. */
export function startBackend(): () => void {
  return isMock() ? startMock() : startReal();
}

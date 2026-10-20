import type { ClientMsg, ServerMsg } from "@shared/protocol";
import { api } from "./api";
import { WS_URL, isMock } from "./env";
import { flush, ingest } from "./ingest";
import { getMock } from "./mock";
import { useTerminal } from "./store";

const HISTORY_MS = 3 * 3600 * 1000;
const BACKOFF_MIN = 500;
const BACKOFF_MAX = 15000;

/** hist (ascending) + live entries at/after `liveFrom`; live wins on overlap. */
function mergeAscending<T>(hist: T[], live: T[], ts: (x: T) => number): T[] {
  if (!live.length) return hist.slice();
  const cut = ts(live[0]);
  const out = hist.filter((x) => ts(x) < cut);
  for (const x of live) out.push(x);
  return out;
}

let historyToken = 0;

/** Fetch the last 3h of bars/odds/fair and install them with NEW arrays (merging anything streamed meanwhile). */
async function loadHistory(connectedAt: number): Promise<void> {
  const token = ++historyToken;
  const to = Date.now() + useTerminal.getState().serverOffsetMs + 60_000;
  const from = to - HISTORY_MS - 60_000;
  const [bars, odds, fair] = await Promise.all([
    api.bars(from, to).catch(() => null),
    api.odds(from, to).catch(() => null),
    api.fair(from, to).catch(() => null),
  ]);
  if (token !== historyToken) return;
  flush(); // make sure queued live messages are in the arrays before we merge
  const st = useTerminal.getState();
  const since = connectedAt - 1000;
  const live = st.series;
  const patch: Partial<typeof live> = {};
  if (bars) patch.bars = mergeAscending(bars, live.bars.filter((b) => b.t >= since), (b) => b.t);
  if (odds) patch.odds = mergeAscending(odds, live.odds.filter((o) => o.ts >= since), (o) => o.ts);
  if (fair) patch.fair = mergeAscending(fair, live.fair.filter((f) => f.ts >= since), (f) => f.ts);
  if (isMock()) {
    const t = getMock().ticks(from, to);
    patch.ticks = mergeAscending(t, live.ticks.filter((x) => x.ts >= since), (x) => x.ts);
  }
  st.setHistory(patch);
  if (!useTerminal.getState().settings) {
    try {
      useTerminal.getState().applySnapshot(await api.state());
    } catch {}
  }
}

function startMock(): () => void {
  const mock = getMock();
  const st = useTerminal;
  st.getState().applySnapshot(mock.snapshot());
  st.setState({ connected: true });
  const opened = Date.now();
  void loadHistory(opened);
  const off = mock.subscribe((m: ServerMsg) => ingest(m));
  return () => {
    off();
    st.setState({ connected: false });
  };
}

function startReal(): () => void {
  let ws: WebSocket | null = null;
  let attempt = 0;
  let stopped = false;
  let retry: ReturnType<typeof setTimeout> | null = null;
  let ping: ReturnType<typeof setInterval> | null = null;

  const scheduleReconnect = () => {
    if (stopped) return;
    const base = Math.min(BACKOFF_MAX, BACKOFF_MIN * 2 ** attempt);
    const delay = Math.round(base * (0.85 + Math.random() * 0.3));
    attempt++;
    retry = setTimeout(connect, delay);
  };

  const connect = () => {
    if (stopped) return;
    try {
      ws = new WebSocket(WS_URL);
    } catch {
      scheduleReconnect();
      return;
    }
    const sock = ws;
    sock.onopen = () => {
      attempt = 0;
      useTerminal.setState({ connected: true });
      void loadHistory(Date.now() + useTerminal.getState().serverOffsetMs);
      ping = setInterval(() => {
        if (sock.readyState === WebSocket.OPEN) {
          const m: ClientMsg = { type: "ping", ts: Date.now() };
          sock.send(JSON.stringify(m));
        }
      }, 15000);
    };
    sock.onmessage = (ev) => {
      try {
        ingest(JSON.parse(ev.data as string) as ServerMsg);
      } catch {}
    };
    sock.onclose = () => {
      if (ping) clearInterval(ping);
      ping = null;
      useTerminal.setState({ connected: false });
      if (ws === sock) ws = null;
      scheduleReconnect();
    };
    sock.onerror = () => {
      try {
        sock.close();
      } catch {}
    };
  };

  connect();
  return () => {
    stopped = true;
    if (retry) clearTimeout(retry);
    if (ping) clearInterval(ping);
    if (ws) {
      ws.onclose = null;
      try {
        ws.close();
      } catch {}
    }
    useTerminal.setState({ connected: false });
  };
}

/** Start the data backend (WebSocket or mock). Returns a stop function. */
export function startBackend(): () => void {
  return isMock() ? startMock() : startReal();
}

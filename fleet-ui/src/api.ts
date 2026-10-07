// The owner API this page uses (docs/FLEET_UI_CONTRACT.md). Same origin, so the
// Tailscale owner auth comes with the request; errors come back as {"detail": "..."}.
import type { ApiFleet, ApiWorker, Job } from './fleet';

export class ApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export interface FleetEvent {
  key: string;
  ts: string;
  worker_id: string | null;
  who: string;
  tone: 'ok' | 'hot' | 'off' | 'fg' | string;
  text: string;
}

export interface EventsAnswer { events: FleetEvent[]; server_time?: string }

export interface RebootAnswer { worker_id: string; reboot_id: string; requested_at: string }

const TIMEOUT_MS = 10000;

function detailOf(body: unknown, status: number): string {
  const d = (body as { detail?: unknown } | null)?.detail;
  if (typeof d === 'string' && d) return d;
  if (Array.isArray(d)) {
    const msgs = d.map((x) => (x && typeof x === 'object' && 'msg' in x ? String((x as { msg: unknown }).msg) : String(x)));
    if (msgs.length) return msgs.join('; ');
  }
  return `request failed (HTTP ${status})`;
}

async function call<T>(method: 'GET' | 'POST', path: string, body?: unknown): Promise<T> {
  const ctl = new AbortController();
  const timer = window.setTimeout(() => ctl.abort(), TIMEOUT_MS);
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      credentials: 'same-origin',
      cache: 'no-store',
      headers: body === undefined ? { Accept: 'application/json' } : { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctl.signal,
    });
  } catch {
    throw new ApiError(ctl.signal.aborted ? 'the host did not answer in time' : 'cannot reach the host', 0);
  } finally {
    window.clearTimeout(timer);
  }
  let data: unknown = null;
  try { data = await res.json(); } catch { /* empty or non-JSON body */ }
  if (!res.ok) throw new ApiError(detailOf(data, res.status), res.status);
  return data as T;
}

/** GET /api/fleet */
export const fetchFleet = () => call<ApiFleet>('GET', '/api/fleet');

/** POST /api/workers/{id}/role  body {"role": "<role id>"} */
export const setRole = (id: string, role: Job) =>
  call<ApiWorker>('POST', `/api/workers/${encodeURIComponent(id)}/role`, { role });

/** POST /api/workers/{id}/reboot  (no body) */
export const reboot = (id: string) =>
  call<RebootAnswer>('POST', `/api/workers/${encodeURIComponent(id)}/reboot`);

/** GET /api/fleet/events?since=<ISO>&limit=<n>  (newest first; `since` is inclusive) */
export const fetchEvents = (since?: string) => {
  const q = since ? `?since=${encodeURIComponent(since)}&limit=100` : '?limit=20';
  return call<EventsAnswer>('GET', `/api/fleet/events${q}`);
};

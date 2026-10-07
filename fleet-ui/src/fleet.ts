// Fleet model for the 3D page: the node shape the views read every frame, the role
// list (from GET /api/fleet), and the mapping from the API's worker rows to nodes.

/** A role id as the host names it ("idle", "backtest", "model_search", "train", "trade"). */
export type Job = string;

export interface Role { id: Job; name: string; short: string }

export type Boot = 'flash' | 'ssd' | 'hdd' | 'unknown';

export interface FleetNode {
  id: string;
  name: string;
  /** desired_role: the role the owner set. */
  job: Job;
  online: boolean;
  /** CPU percent (0 when offline). */
  cpu: number;
  /** RAM percent (0 when offline). */
  ram: number;
  temp: number | null;
  boot: Boot;
  /** Percent of rated life used, when the boot disk reports it. */
  wear: number | null;
  /** Kept for the views' animation phase; always 0 for real data. */
  bias: number;
  /** Last 48 CPU samples, one per poll, kept client side. */
  hist: number[];
  /** 1 while a reboot is pending, else 0. */
  down: number;
  /** Seconds since the last heartbeat (-1 when never seen). */
  seen: number;
  switching: boolean;
  rebooting: boolean;
  canReboot: boolean;
  enabled: boolean;
  gbWritten: number | null;
  /** Games of this worker's trade jobs ("KC @ LV"). */
  games: string[];
  host: string | null;
}

export const HIST_LEN = 48;

/** The host's role list; replaced by `roles` from each /api/fleet answer. */
export const DEFAULT_ROLES: Role[] = [
  { id: 'idle', name: 'Idle', short: 'Idle' },
  { id: 'backtest', name: 'Backtest', short: 'Backtest' },
  { id: 'model_search', name: 'Model search', short: 'Search' },
  { id: 'train', name: 'Training', short: 'Train' },
  { id: 'trade', name: 'Trading', short: 'Trade' },
];

/** The current role list (a live binding: importers always see the latest). */
export let JOBS: Role[] = DEFAULT_ROLES;

export function setJobs(roles: Role[]) {
  if (roles.length) JOBS = roles;
}

export const jobName = (j: Job) => JOBS.find((x) => x.id === j)?.name ?? j;
export const jobShort = (j: Job) => JOBS.find((x) => x.id === j)?.short ?? j;

export type Status = 'off' | 'hot' | 'idle' | 'ok';
export const HOT_AT = 72;

export function statusOf(n: FleetNode): Status {
  if (!n.online) return 'off';
  if (n.temp !== null && n.temp >= HOT_AT) return 'hot';
  if (n.job === 'idle') return 'idle';
  return 'ok';
}

/** A stable value in [0, 1) for any string id (FNV-1a, then a murmur3 finaliser so
 * ids that differ only in their last character still land far apart). */
export function hashOf(id: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < id.length; i++) {
    h ^= id.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  h ^= h >>> 16;
  h = Math.imul(h, 0x85ebca6b);
  h ^= h >>> 13;
  h = Math.imul(h, 0xc2b2ae35);
  h ^= h >>> 16;
  return (h >>> 0) / 4294967296;
}

/* ---------- API shapes (docs/PROTOCOL.md "Fleet UI additions") ---------- */

export interface ApiJob { id: string; kind: string; status: string; progress: number | null; game?: string | null }

export interface ApiWorker {
  id: string;
  name: string;
  online: boolean;
  desired_role: string;
  reported_role: string | null;
  switching: boolean;
  enabled: boolean;
  cpu_pct: number | null;
  ram_used_mb: number | null;
  ram_total_mb: number | null;
  ram_pct?: number | null;
  temp_c?: number | null;
  boot_media?: string | null;
  wear_pct?: number | null;
  disk_gb_written?: number | null;
  seconds_since_heartbeat?: number | null;
  can_reboot?: boolean;
  rebooting?: boolean;
  hostname: string | null;
  last_heartbeat_at: string | null;
  current_jobs: ApiJob[];
}

export interface ApiFleet {
  workers: ApiWorker[];
  roles?: Role[];
  online_after_seconds?: number;
  server_time?: string;
}

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const clampPct = (v: number) => Math.max(0, Math.min(100, v));
const BOOTS: Boot[] = ['flash', 'ssd', 'hdd', 'unknown'];

function ramPct(w: ApiWorker): number | null {
  const p = num(w.ram_pct);
  if (p !== null) return p;
  const used = num(w.ram_used_mb), total = num(w.ram_total_mb);
  return used !== null && total ? (used / total) * 100 : null;
}

/** Map the API's workers to nodes, carrying each node's CPU history over from `prev`. */
export function toNodes(workers: ApiWorker[], prev: FleetNode[]): FleetNode[] {
  const before = new Map(prev.map((n) => [n.id, n]));
  return workers.map((w) => {
    const online = !!w.online;
    const cpu = online ? clampPct(num(w.cpu_pct) ?? 0) : 0;
    const old = before.get(w.id);
    const hist = old ? [...old.hist.slice(-(HIST_LEN - 1)), cpu] : Array.from({ length: HIST_LEN }, () => cpu);
    const games = [...new Set(
      (w.current_jobs ?? []).filter((j) => j.kind === 'trade' && j.game).map((j) => String(j.game)),
    )];
    const boot = BOOTS.includes(w.boot_media as Boot) ? (w.boot_media as Boot) : 'unknown';
    const rebooting = !!w.rebooting;
    const seen = num(w.seconds_since_heartbeat);
    const wear = num(w.wear_pct);
    return {
      id: w.id,
      name: w.name || w.id,
      job: w.desired_role,
      online,
      cpu,
      ram: online ? clampPct(ramPct(w) ?? 0) : 0,
      temp: num(w.temp_c),
      boot,
      wear: wear === null ? null : clampPct(wear),
      bias: 0,
      hist,
      down: rebooting ? 1 : 0,
      seen: seen === null ? -1 : Math.max(0, Math.round(seen)),
      switching: !!w.switching,
      rebooting,
      canReboot: !!w.can_reboot,
      enabled: w.enabled !== false,
      gbWritten: num(w.disk_gb_written),
      games,
      host: w.hostname ?? null,
    };
  });
}

import { useCallback, useEffect, useRef, useState } from 'react';
import type { CSSProperties, FormEvent } from 'react';
import Scene, { layoutKeyOf } from './Scene';
import { DEFAULT_ROLES, HOT_AT, jobName, jobShort, setJobs, toNodes } from './fleet';
import type { FleetNode, Job, Role } from './fleet';
import { ApiError, fetchEvents, fetchFleet, reboot, setRole } from './api';
import type { FleetEvent } from './api';
import { parseCommand } from './commands';
import Inspector from './Inspector';
import { tone, useHeightVar } from './ui';

const POLL_MS = 3000;
const LIVE_MS = 10000;
const SHOW_EVENTS = 5;
const EV_TONE: Record<string, string> = { ok: 'var(--ok)', hot: 'var(--hot)', off: 'var(--off)', fg: 'var(--fg)' };

/** A feed line; `ts` is milliseconds on the server's clock. */
interface Ev { key: string; ts: number; who: string; text: string; tone: string }

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));
const isAuth = (e: unknown) => e instanceof ApiError && (e.status === 401 || e.status === 403);

export default function App() {
  const [nodes, setNodes] = useState<FleetNode[]>([]);
  const [roles, setRoles] = useState<Role[]>(DEFAULT_ROLES);
  const [loaded, setLoaded] = useState(false);
  const [tried, setTried] = useState(false);
  const [lastOk, setLastOk] = useState<number | null>(null);
  const [signedOut, setSignedOut] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [sel, setSel] = useState<string | null>(null);
  const [cmd, setCmd] = useState('');
  const [serverEvents, setServerEvents] = useState<Ev[]>([]);
  const [localEvents, setLocalEvents] = useState<Ev[]>([]);
  const [pending, setPending] = useState<string[]>([]);

  const nodesRef = useRef(nodes);
  const consoleRef = useHeightVar('console-h');
  nodesRef.current = nodes;
  const skew = useRef(0);
  const onlineAfter = useRef(30);
  const since = useRef<string | undefined>(undefined);
  const seenKeys = useRef(new Set<string>());
  const seq = useRef(0);
  const fleetBusy = useRef(false);
  const fleetAgain = useRef(false);
  const eventsBusy = useRef(false);

  const log = useCallback((who: string, text: string, t = 'fg') => {
    const ev = { key: `l:${seq.current++}`, ts: Date.now() + skew.current, who, text, tone: t };
    setLocalEvents((e) => [ev, ...e].slice(0, 20));
  }, []);

  const refreshFleet = useCallback(async () => {
    if (fleetBusy.current) { fleetAgain.current = true; return; }
    fleetBusy.current = true;
    try {
      const data = await fetchFleet();
      if (data.server_time) { const s = Date.parse(data.server_time); if (Number.isFinite(s)) skew.current = s - Date.now(); }
      if (typeof data.online_after_seconds === 'number') onlineAfter.current = data.online_after_seconds;
      const r = Array.isArray(data.roles) && data.roles.length ? data.roles : DEFAULT_ROLES;
      setJobs(r);
      setRoles((old) => (old.map((x) => `${x.id}:${x.name}:${x.short}`).join() === r.map((x) => `${x.id}:${x.name}:${x.short}`).join() ? old : r));
      const prev = nodesRef.current;
      const next = toNodes(Array.isArray(data.workers) ? data.workers : [], prev);
      // online/offline and temperature crossings are not stored by the host: derive them
      const before = new Map(prev.map((n) => [n.id, n]));
      for (const n of next) {
        const p = before.get(n.id);
        if (!p) continue;
        if (n.online && !p.online) log(n.name, 'Back online', 'ok');
        if (!n.online && p.online) log(n.name, `Went offline (no heartbeat for ${onlineAfter.current} s)`, 'off');
        if (n.online && n.temp !== null && n.temp >= HOT_AT && (p.temp === null || p.temp < HOT_AT)) log(n.name, `Passed ${HOT_AT} °C`, 'hot');
      }
      setNodes(next);
      setLoaded(true);
      setSignedOut(false);
      setLastOk(Date.now());
    } catch (e) {
      if (isAuth(e)) setSignedOut(true);
    } finally {
      setTried(true);
      fleetBusy.current = false;
      if (fleetAgain.current) { fleetAgain.current = false; void refreshFleet(); }
    }
  }, [log]);

  const refreshEvents = useCallback(async () => {
    if (eventsBusy.current) return;
    eventsBusy.current = true;
    try {
      const data = await fetchEvents(since.current);
      const fresh: Ev[] = [];
      let newest = since.current ? Date.parse(since.current) : -Infinity;
      for (const e of (Array.isArray(data.events) ? data.events : []) as FleetEvent[]) {
        const ts = Date.parse(e.ts);
        if (Number.isFinite(ts) && ts > newest) { newest = ts; since.current = e.ts; }
        if (seenKeys.current.has(e.key)) continue;
        seenKeys.current.add(e.key);
        fresh.push({ key: e.key, ts: Number.isFinite(ts) ? ts : Date.now() + skew.current, who: e.who || 'fleet', text: e.text, tone: e.tone });
      }
      if (seenKeys.current.size > 1000) seenKeys.current = new Set([...seenKeys.current].slice(-500));
      if (fresh.length) setServerEvents((old) => [...fresh, ...old].sort((a, b) => b.ts - a.ts).slice(0, 30));
    } catch (e) {
      if (isAuth(e)) setSignedOut(true);
    } finally {
      eventsBusy.current = false;
    }
  }, []);

  useEffect(() => {
    void refreshFleet();
    void refreshEvents();
    const poll = window.setInterval(() => { void refreshFleet(); void refreshEvents(); }, POLL_MS);
    const tick = window.setInterval(() => setNow(Date.now()), 2000);
    return () => { window.clearInterval(poll); window.clearInterval(tick); };
  }, [refreshFleet, refreshEvents]);

  const afterAction = () => { void refreshFleet(); void refreshEvents(); };
  const withPending = async (ids: string[], work: () => Promise<void>) => {
    setPending((p) => [...p, ...ids]);
    try { await work(); } finally { setPending((p) => p.filter((x) => !ids.includes(x))); afterAction(); }
  };

  /** Set `role` on the targets. Moving into or out of trade asks first. */
  const assign = async (targets: FleetNode[], role: Job) => {
    if (!targets.length) return;
    const todo = targets.filter((n) => n.job !== role);
    if (!todo.length) { log(targets.length === 1 ? targets[0].name : 'fleet', `Already on ${jobName(role)}`); return; }
    const crossing = todo.filter((n) => (role === 'trade') !== (n.job === 'trade'));
    if (crossing.length) {
      const names = crossing.map((n) => n.name).join(', ');
      const head = role === 'trade' ? `Move ${names} to ${jobName('trade')}?` : `Move ${names} off ${jobName('trade')} (to ${jobName(role)})?`;
      const ok = window.confirm(`${head}\n\nA trade worker places bets for its assignments (paper or live, as set on the Trading page). Moving a worker off trade cancels its open orders.`);
      if (!ok) { log('console', 'Cancelled, nothing changed'); return; }
    }
    await withPending(todo.map((n) => n.id), async () => {
      const results = await Promise.allSettled(todo.map((n) => setRole(n.id, role)));
      const moved = todo.filter((_, i) => results[i].status === 'fulfilled');
      if (moved.length === 1) log(moved[0].name, `Moving to ${jobName(role)}`, 'ok');
      else if (moved.length > 1) log('fleet', `Moving ${moved.length} machines to ${jobName(role)}`, 'ok');
      results.forEach((r, i) => { if (r.status === 'rejected') log(todo[i].name, `Not moved: ${errText(r.reason)}`, 'hot'); });
    });
  };

  const restart = async (n: FleetNode) => {
    await withPending([n.id], async () => {
      try {
        await reboot(n.id);
        log(n.name, 'Reboot requested', 'off');
      } catch (e) {
        log(n.name, `Reboot refused: ${errText(e)}`, 'hot');
      }
    });
  };

  const chips = ['search on idle', 'stop search', 'backtest on idle', ...(nodes.length ? [`reboot ${nodes[0].name}`] : [])];

  const run = (raw: string) => {
    if (!raw.trim()) return;
    const c = parseCommand(raw, nodes, roles);
    setCmd('');
    switch (c.kind) {
      case 'reboot':
        c.targets.forEach((n) => void restart(n));
        return;
      case 'stop': {
        const on = nodes.filter((n) => n.online && n.job === c.role);
        if (!on.length) log('console', `No online workers on ${jobName(c.role)}`);
        else void assign(on, 'idle');
        return;
      }
      case 'onIdle': {
        const idle = nodes.filter((n) => n.online && n.job === 'idle');
        if (!idle.length) log('console', 'No idle workers online');
        else void assign(idle, c.role);
        return;
      }
      case 'assign':
        void assign(c.targets, c.role);
        return;
      default:
        log('console', `${c.reason} Try: ${chips.slice(0, 3).join(' · ')}`, 'hot');
    }
  };
  const submit = (e: FormEvent) => { e.preventDefault(); run(cmd); };

  const online = nodes.filter((n) => n.online);
  const hot = online.filter((n) => n.temp !== null && n.temp >= HOT_AT).length;
  const warm = online.filter((n) => n.temp !== null);
  const avg = warm.length ? warm.reduce((s, n) => s + (n.temp ?? 0), 0) / warm.length : null;
  const node = nodes.find((n) => n.id === sel) ?? nodes[0] ?? null;
  const live = lastOk !== null && now - lastOk < LIVE_MS;
  const feed = [...serverEvents, ...localEvents].sort((a, b) => b.ts - a.ts).slice(0, SHOW_EVENTS);
  const layoutKey = layoutKeyOf(nodes);

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <h1>polymarket-fleet</h1>
          <span className={'conn' + (live ? ' is-live' : '')} role="status">{live ? 'Live' : tried ? 'Disconnected' : 'Connecting'}</span>
        </div>
        {!signedOut && <dl className="stats">
          <div><dt>Online</dt><dd>{online.length}/{nodes.length}</dd></div>
          <div><dt>Working</dt><dd>{online.filter((n) => n.job !== 'idle').length}</dd></div>
          <div><dt>Hot</dt><dd className={hot ? 'warn' : ''}>{hot}</dd></div>
          <div><dt>Avg temp</dt><dd>{avg === null ? 'n/a' : `${avg.toFixed(0)}°C`}</dd></div>
        </dl>}
        <nav className="links" aria-label="Other pages">
          <a href="/fleet/list">List view</a>
          <a href="/">Dashboard</a>
        </nav>
      </header>

      {signedOut ? (
        <main className="stage">
          <section className="panel blocker" role="alert">
            <p>Not signed in: open this page through the fleet's Tailscale address.</p>
          </section>
        </main>
      ) : (
        <main className="stage">
          <Scene nodesRef={nodesRef} layoutKey={layoutKey} selected={node?.id ?? null} onSelect={setSel} />
          <p className="hint">Drag to rotate · scroll to zoom · click a machine</p>

          <section className="panel roster" aria-label="Machines">
            <h2>Machines</h2>
            {nodes.length ? (
              <ul>
                {nodes.map((n) => (
                  <li key={n.id}>
                    <button type="button" className="row" style={tone(n)} aria-pressed={node?.id === n.id} onClick={() => setSel(n.id)}>
                      <span className={'dot' + (n.online ? '' : ' off')} />
                      <span className="id">{n.name}</span>
                      <span className="job">{n.rebooting ? 'Rebooting' : n.online ? jobShort(n.job) : 'Offline'}</span>
                      <span className="mini"><i style={{ width: n.cpu + '%' }} /></span>
                      <span className="t">{n.online && n.temp !== null ? Math.round(n.temp) + '°' : 'n/a'}</span>
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="empty">
                {loaded ? <>No workers yet. Enroll one from <a href="/settings">Settings</a>.</> : tried ? 'Cannot reach the host yet.' : 'Connecting to the host.'}
              </p>
            )}
          </section>

          {node && (
            <Inspector node={node} roles={roles} busy={pending.includes(node.id)}
              onRole={(role) => void assign([node], role)} onReboot={() => void restart(node)} />
          )}

          <section className="panel console" aria-label="Command console" ref={consoleRef}>
            <form className="cmd" onSubmit={submit}>
              <label htmlFor="fleet-cmd">Command for the fleet</label>
              <span className="prompt" aria-hidden="true">&gt;</span>
              <input id="fleet-cmd" value={cmd} onChange={(e) => setCmd(e.target.value)} placeholder="search on idle" autoComplete="off" spellCheck={false} />
              <button type="submit" className="btn go">Run</button>
            </form>
            <div className="chips">
              {chips.map((c) => <button key={c} type="button" onClick={() => run(c)}>{c}</button>)}
            </div>
            <ul className="feed" aria-live="polite">
              {feed.map((e) => (
                <li key={e.key} style={{ '--c': EV_TONE[e.tone] ?? 'var(--fg)' } as CSSProperties}>
                  <time dateTime={new Date(e.ts).toISOString()}>{new Date(e.ts).toLocaleTimeString('en-GB')}</time>
                  <span className="who">{e.who}</span><span className="txt" title={e.text}>{e.text}</span>
                </li>
              ))}
            </ul>
          </section>
        </main>
      )}
    </div>
  );
}

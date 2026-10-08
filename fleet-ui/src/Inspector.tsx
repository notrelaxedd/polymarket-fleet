// The selected machine: status, facts, CPU sparkline, meters, role buttons, reboot.
import { jobName, statusOf } from './fleet';
import type { FleetNode, Job, Role } from './fleet';
import { BOOT_WORD, fmtAgo, tone } from './ui';

const STATUS_WORD = { ok: 'Working', hot: 'Running hot', idle: 'Idle', off: 'Offline' } as const;
const WRITTEN_FULL_GB = 50;
const W = 276, H = 56;

interface Props {
  node: FleetNode;
  roles: Role[];
  busy: boolean;
  onRole: (role: Job) => void;
  onReboot: () => void;
}

interface Meter { k: string; pct: number; text: string }

function meters(n: FleetNode): Meter[] {
  const pct = (v: number) => Math.max(0, Math.min(100, v));
  const out: Meter[] = [
    { k: 'CPU', pct: n.online ? n.cpu : 0, text: n.online ? `${Math.round(n.cpu)}%` : 'n/a' },
    { k: 'Memory', pct: n.online ? n.ram : 0, text: n.online ? `${Math.round(n.ram)}%` : 'n/a' },
    { k: 'Temp', pct: n.online && n.temp !== null ? pct(n.temp) : 0, text: n.online && n.temp !== null ? `${Math.round(n.temp)}°C` : 'n/a' },
  ];
  if (n.wear !== null) out.push({ k: 'Wear', pct: pct(n.wear), text: `${Math.round(n.wear)}%` });
  else if (n.gbWritten !== null) out.push({ k: 'Written', pct: pct((n.gbWritten / WRITTEN_FULL_GB) * 100), text: `${n.gbWritten < 10 ? n.gbWritten.toFixed(1) : Math.round(n.gbWritten)} GB` });
  else out.push({ k: 'Written', pct: 0, text: 'not reported' });
  return out;
}

function rebootBlock(n: FleetNode): string | null {
  if (n.rebooting) return 'Reboot requested; waiting for the machine to come back.';
  if (!n.online) return 'Offline: a reboot needs a heartbeat.';
  if (!n.canReboot) return 'Re-run install.sh to enable reboot.';
  return null;
}

export default function Inspector({ node, roles, busy, onRole, onReboot }: Props) {
  const st = statusOf(node);
  const hist = node.hist.length > 1 ? node.hist : [node.cpu, node.cpu];
  const pts = hist.map((v, i) => [(i / (hist.length - 1)) * W, H - 4 - (v / 100) * (H - 10)] as const);
  const line = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
  const last = pts[pts.length - 1];
  const running = node.online ? jobName(node.job) + (node.games.length ? `: ${node.games.join(', ')}` : '') : 'Nothing';
  const blocked = rebootBlock(node);
  const pill = node.rebooting ? 'Rebooting' : !node.enabled ? 'Disabled' : STATUS_WORD[st];

  return (
    <section className="panel inspector" aria-label="Selected machine" style={tone(node)}>
      <div className="name">
        <h3>{node.name}</h3>
        <span className={'pill' + (st === 'hot' ? ' solid' : '')}>{pill}</span>
      </div>
      <dl className="facts">
        <div><dt>Running</dt><dd>{running}</dd></div>
        <div><dt>Heartbeat</dt><dd>{fmtAgo(node.seen)}</dd></div>
        <div><dt>Boots from</dt><dd>{BOOT_WORD[node.boot]}</dd></div>
        <div><dt>Host</dt><dd className="host">{node.host ?? 'n/a'}</dd></div>
      </dl>
      <svg className="spark" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label="CPU load over the last 48 updates">
        <line x1="0" y1={H - 4} x2={W} y2={H - 4} stroke="var(--line)" />
        <line x1="0" y1={(H - 4) / 2 + 1} x2={W} y2={(H - 4) / 2 + 1} stroke="var(--line)" strokeDasharray="2 4" />
        <path d={`${line} L${W} ${H - 4} L0 ${H - 4} Z`} fill="var(--c)" opacity="0.16" />
        <path d={line} fill="none" stroke="var(--c)" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
        <circle cx={last[0] - 2} cy={last[1]} r="3" fill="var(--c)" />
      </svg>
      <div>
        {meters(node).map((m) => (
          <div className="meter" key={m.k}>
            <span className="k">{m.k}</span>
            <span className="bar"><i style={{ width: m.pct + '%' }} /></span>
            <span className="v">{m.text}</span>
          </div>
        ))}
      </div>
      <div className="moves" aria-busy={busy}>
        {roles.map((r) => {
          const on = node.job === r.id;
          return (
            <button key={r.id} type="button" className="btn" disabled={on} title={r.name}
              onClick={() => { if (!busy) onRole(r.id); }}>
              {on ? (node.switching ? `Switching to ${r.short}` : `On ${r.short}`) : r.short}
            </button>
          );
        })}
        <button type="button" className="btn muted" disabled={!!blocked || busy} onClick={onReboot}
          aria-describedby={blocked ? 'reboot-why' : undefined}>
          {node.rebooting ? 'Rebooting' : 'Reboot'}
        </button>
        {blocked && <p className="why" id="reboot-why">{blocked}</p>}
      </div>
    </section>
  );
}

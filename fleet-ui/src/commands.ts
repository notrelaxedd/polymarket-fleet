// The command bar's little language:
//   reboot|restart <workers>      stop|pause <role>      <role> on idle
//   <role> <workers>              <workers> to <role>
// Workers are named by name (any case), by id, or by a bare number that matches the
// trailing digits of a name ("reboot 5" -> box5 or node-05).
import type { FleetNode, Job, Role } from './fleet';

export type Command =
  | { kind: 'reboot'; targets: FleetNode[] }
  | { kind: 'stop'; role: Job }
  | { kind: 'onIdle'; role: Job }
  | { kind: 'assign'; targets: FleetNode[]; role: Job }
  | { kind: 'unknown'; reason: string };

const SYNONYMS: [string, Job][] = [
  ['model[\\s_-]*search(?:es)?', 'model_search'],
  ['search(?:es)?', 'model_search'],
  ['backtests?', 'backtest'],
  ['train(?:ing)?', 'train'],
  ['trad(?:e|es|ing)', 'trade'],
  ['idle', 'idle'],
];
const FILLER = new Set(['reboot', 'restart', 'stop', 'pause', 'on', 'to', 'and', 'all', 'the', 'move', 'put', 'set',
  'run', 'start', 'worker', 'workers', 'machine', 'machines', 'box', 'boxes', 'node', 'nodes', 'role', 'please']);

const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const trailing = (s: string) => { const m = /(\d+)$/.exec(s); return m ? parseInt(m[1], 10) : null; };

/** Role mentions in order of appearance, with the text left after removing them. */
function findRoles(text: string, roles: Role[]) {
  const known = new Set(roles.map((r) => r.id));
  const pats: [string, Job][] = SYNONYMS.filter(([, id]) => known.has(id));
  roles.forEach((r) => [r.id, r.name, r.short].forEach((w) => pats.push([esc(w.toLowerCase()).replace(/\s+/g, '\\s+'), r.id])));
  pats.sort((a, b) => b[0].length - a[0].length);
  const found: { at: number; id: Job }[] = [];
  let rest = text;
  for (const [p, id] of pats) {
    rest = rest.replace(new RegExp(`(^|[\\s,])(${p})(?=$|[\\s,])`, 'g'), (m, pre: string, _w: string, at: number) => {
      found.push({ at: at + pre.length, id });
      return pre + ' '.repeat(m.length - pre.length);
    });
  }
  found.sort((a, b) => a.at - b.at);
  return { found: found.map((f) => f.id), rest };
}

export function parseCommand(raw: string, nodes: FleetNode[], roles: Role[]): Command {
  const text = raw.toLowerCase().trim().replace(/\s+/g, ' ');
  if (!text) return { kind: 'unknown', reason: 'Type a command.' };
  const byName = new Map(nodes.map((n) => [n.name.toLowerCase(), n]));
  const byId = new Map(nodes.map((n) => [n.id.toLowerCase(), n]));

  // worker names and ids first (a worker could be called "trade1"), then role words
  const targets: FleetNode[] = [];
  const add = (n: FleetNode) => { if (!targets.includes(n)) targets.push(n); };
  const kept: string[] = [];
  for (const tok of text.split(/[\s,]+/)) {
    const n = byName.get(tok) ?? byId.get(tok);
    if (n) add(n); else kept.push(tok);
  }
  const { found, rest } = findRoles(kept.join(' '), roles);
  const unknown: string[] = [];
  for (const tok of rest.split(/[\s,]+/).filter(Boolean)) {
    if (FILLER.has(tok)) continue;
    if (/^\d+$/.test(tok)) {
      const want = parseInt(tok, 10);
      const hits = nodes.filter((n) => trailing(n.name) === want);
      if (hits.length) { hits.forEach(add); continue; }
    }
    unknown.push(tok);
  }
  if (unknown.length) return { kind: 'unknown', reason: `No worker or role called "${unknown.join(' ')}".` };

  const verb = /^(reboot|restart)\b/.test(text) || /\b(reboot|restart)\b/.test(text) ? 'reboot'
    : /\b(stop|pause)\b/.test(text) ? 'stop' : null;
  if (verb === 'reboot') {
    return targets.length ? { kind: 'reboot', targets } : { kind: 'unknown', reason: 'Name the worker to reboot.' };
  }
  if (verb === 'stop') {
    const role = found.find((r) => r !== 'idle');
    if (role && !targets.length) return { kind: 'stop', role };
    if (targets.length) return { kind: 'assign', targets, role: 'idle' };
    return { kind: 'unknown', reason: 'Say which role to stop.' };
  }
  if (!targets.length && /\bon idle\b/.test(text)) {
    const role = found.find((r) => r !== 'idle');
    if (role) return { kind: 'onIdle', role };
  }
  if (targets.length && found.length) {
    const role = /\bto\b/.test(text) ? found[found.length - 1] : found[0];
    return { kind: 'assign', targets, role };
  }
  if (targets.length) return { kind: 'unknown', reason: 'Say which role, e.g. "backtest ' + targets[0].name + '".' };
  return { kind: 'unknown', reason: `Nothing to do for "${raw.trim()}".` };
}

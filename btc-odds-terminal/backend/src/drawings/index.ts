// Chart drawings + stickers persisted in table `drawings` (kind, data JSON, locked). OWNER: drawings.
//
// Routes: GET/POST/PUT(replace all)/DELETE(all) /api/drawings, PUT/DELETE /api/drawings/:id.
// Broadcasts `drawings` (full list) after every change.
//
// - POST accepts an optional client `id` (kept when it is a safe, unused id; else a new id is minted).
// - PUT /api/drawings (replace all) keeps the ids it is given (used to bring back an undone "delete all").
// - PUT / DELETE /api/drawings/:id return 404 for unknown ids (the client then re-creates or replaces).
// Every row is validated (kind, exact point count, finite numbers, one scale for all points, sticker kind).

import { randomUUID } from "node:crypto";
import { z } from "zod";
import type { Drawing, DrawingInput, DrawingKind } from "../../../shared/protocol.ts";
import type { BackendModule, DrawingsService, ModuleDeps } from "../modules/types.ts";
import { badRequest, notFound, zodError } from "../server/errors.ts";

export const MAX_DRAWINGS = 500;
export const MAX_LABEL_LEN = 500;

const KINDS = ["trend", "ray", "hline", "vline", "rect", "fib", "text", "measure", "sticker"] as const satisfies readonly DrawingKind[];
const STICKERS = ["target", "rocket", "warning", "check", "cross", "bull", "bear", "lightning"] as const;
const ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

export function pointCount(kind: DrawingKind): number {
  return kind === "hline" || kind === "vline" || kind === "text" || kind === "sticker" ? 1 : 2;
}

const Point = z
  .object({
    time: z.number().finite().min(0),
    price: z.number().finite(),
    scale: z.enum(["btc", "odds"]),
  })
  .strict();

const Fields = z
  .object({
    kind: z.enum(KINDS),
    points: z.array(Point).min(1).max(2),
    color: z.string().regex(/^#[0-9a-fA-F]{3,8}$/, "CSS hex color"),
    locked: z.boolean(),
    label: z.string().max(MAX_LABEL_LEN).nullable(),
    sticker: z.enum(STICKERS).nullable().optional(),
    lineWidth: z.number().finite().positive().max(10).optional(),
    dashed: z.boolean().optional(),
  })
  .strict();

const CreateSchema = Fields.partial({ locked: true, label: true }).extend({ id: z.string().optional() });
const UpdateSchema = Fields.partial();
const FullSchema = Fields.extend({
  id: z.string().regex(ID_RE, "id: letters, digits, _ and - (max 64)"),
  createdTs: z.number().finite().optional(),
  updatedTs: z.number().finite().optional(),
});

/** whole-drawing checks zod cannot express per field */
function checkShape(d: DrawingInput): DrawingInput {
  const n = pointCount(d.kind);
  if (d.points.length !== n) throw badRequest(`points: ${d.kind} takes exactly ${n} point${n > 1 ? "s" : ""}`);
  const scale = d.points[0].scale;
  if (!d.points.every((p) => p.scale === scale)) throw badRequest("points: every point must use the same scale");
  if (scale === "btc" && d.points.some((p) => p.price <= 0)) throw badRequest("points: BTC price must be > 0");
  if (d.kind === "sticker" && !d.sticker) throw badRequest("sticker: required for kind sticker");
  return d;
}

function normInput(b: z.infer<typeof Fields>): DrawingInput {
  const out: DrawingInput = {
    kind: b.kind,
    points: b.points.map((p) => ({ time: p.time, price: p.price, scale: p.scale })),
    color: b.color,
    locked: b.locked,
    label: b.label ?? null,
  };
  if (b.sticker !== undefined) out.sticker = b.sticker;
  if (b.lineWidth !== undefined) out.lineWidth = b.lineWidth;
  if (b.dashed !== undefined) out.dashed = b.dashed;
  return checkShape(out);
}

interface Row {
  id: string;
  kind: string;
  data: string;
  locked: number;
  created_ts: number;
  updated_ts: number;
}

export function createDrawings(deps: ModuleDeps): BackendModule<DrawingsService> {
  const db = deps.db.raw;
  const q = {
    all: db.prepare<[], Row>("SELECT * FROM drawings ORDER BY created_ts, id"),
    insert: db.prepare(
      "INSERT INTO drawings (id, kind, data, locked, created_ts, updated_ts) VALUES (@id, @kind, @data, @locked, @created_ts, @updated_ts)",
    ),
    update: db.prepare("UPDATE drawings SET kind=@kind, data=@data, locked=@locked, updated_ts=@updated_ts WHERE id=@id"),
    del: db.prepare<[string]>("DELETE FROM drawings WHERE id = ?"),
    delAll: db.prepare("DELETE FROM drawings"),
  };

  const toRow = (d: Drawing): Row => {
    const { id, kind, locked, createdTs, updatedTs, ...rest } = d;
    return { id, kind, data: JSON.stringify(rest), locked: locked ? 1 : 0, created_ts: createdTs, updated_ts: updatedTs };
  };
  const fromRow = (r: Row): Drawing | null => {
    try {
      const data = JSON.parse(r.data) as Omit<DrawingInput, "kind" | "locked">;
      return { ...data, kind: r.kind as DrawingKind, locked: r.locked === 1, id: r.id, createdTs: r.created_ts, updatedTs: r.updated_ts };
    } catch {
      deps.logger.warn({ id: r.id }, "drawings: unreadable row skipped");
      return null;
    }
  };

  const map = new Map<string, Drawing>();
  for (const r of q.all.all()) {
    const d = fromRow(r);
    if (d) map.set(d.id, d);
  }

  const list = (): Drawing[] => [...map.values()];
  const publish = () => deps.broadcast({ type: "drawings", data: list() });
  /** strictly increasing timestamps so a client can order echoes by updatedTs */
  let lastTs = 0;
  const stamp = () => (lastTs = Math.max(deps.now(), lastTs + 1));

  const create = (body: unknown): Drawing => {
    const r = CreateSchema.safeParse(body);
    if (!r.success) throw zodError(r.error.issues);
    if (map.size >= MAX_DRAWINGS) throw badRequest(`at most ${MAX_DRAWINGS} drawings`);
    const { id: wanted, ...fields } = r.data;
    const input = normInput({ ...fields, locked: fields.locked ?? false, label: fields.label ?? null });
    const id = wanted && ID_RE.test(wanted) && !map.has(wanted) ? wanted : randomUUID();
    const ts = stamp();
    const d: Drawing = { ...input, id, createdTs: ts, updatedTs: ts };
    q.insert.run(toRow(d));
    map.set(id, d);
    publish();
    return d;
  };

  const update = (id: string, body: unknown): Drawing => {
    const cur = map.get(id);
    if (!cur) throw notFound(`no drawing ${id}`);
    const r = UpdateSchema.safeParse(body ?? {});
    if (!r.success) throw zodError(r.error.issues);
    const { id: _i, createdTs, updatedTs: _u, ...curInput } = cur;
    void _i;
    void _u;
    const merged = normInput({ ...curInput, ...r.data } as z.infer<typeof Fields>);
    const d: Drawing = { ...merged, id, createdTs, updatedTs: stamp() };
    q.update.run(toRow(d));
    map.set(id, d);
    publish();
    return d;
  };

  const replaceAll = (body: unknown): Drawing[] => {
    const r = z.array(FullSchema).max(MAX_DRAWINGS).safeParse(body ?? []);
    if (!r.success) throw zodError(r.error.issues);
    const seen = new Set<string>();
    const ts = stamp();
    const next: Drawing[] = r.data.map((x) => {
      if (seen.has(x.id)) throw badRequest(`duplicate id ${x.id}`);
      seen.add(x.id);
      const { id, createdTs, updatedTs: _u, ...fields } = x;
      void _u;
      return { ...normInput(fields), id, createdTs: createdTs ?? map.get(id)?.createdTs ?? ts, updatedTs: ts };
    });
    db.transaction(() => {
      q.delAll.run();
      for (const d of next) q.insert.run(toRow(d));
    })();
    map.clear();
    for (const d of next) map.set(d.id, d);
    publish();
    return list();
  };

  const service: DrawingsService = { list };
  return {
    name: "drawings",
    service,
    register(app) {
      app.get("/api/drawings", async () => list());
      app.post("/api/drawings", async (req) => create(req.body));
      app.put("/api/drawings", async (req) => replaceAll(req.body));
      app.delete("/api/drawings", async () => {
        const deleted = map.size;
        q.delAll.run();
        map.clear();
        publish();
        return { deleted };
      });
      app.put<{ Params: { id: string } }>("/api/drawings/:id", async (req) => update(req.params.id, req.body));
      app.delete<{ Params: { id: string } }>("/api/drawings/:id", async (req) => {
        const id = req.params.id;
        if (!map.has(id)) throw notFound(`no drawing ${id}`);
        q.del.run(id);
        map.delete(id);
        publish();
        return { ok: true as const };
      });
    },
    snapshot: () => ({ drawings: list() }),
  };
}

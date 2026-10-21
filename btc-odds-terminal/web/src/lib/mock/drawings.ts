// Mock drawings store mirroring backend/src/drawings. OWNER: drawings.
// STUB from the scaffold: in-memory CRUD that broadcasts `drawings` (full list). Replace freely
// (keep `createMockDrawings` + MockDrawings).

import type { Drawing, DrawingInput } from "@shared/protocol";
import { ApiError } from "../api-error";
import type { MockCtx, MockDrawings } from "./types";

export function createMockDrawings(ctx: MockCtx): MockDrawings {
  let list: Drawing[] = [];
  let seq = 0;
  const changed = () => ctx.emit({ type: "drawings", data: list });
  const find = (id: string) => {
    const d = list.find((x) => x.id === id);
    if (!d) throw new ApiError(404, "not_found", "no such drawing");
    return d;
  };
  return {
    name: "drawings",
    list: () => list,
    routes: [
      { method: "GET", path: "/drawings", handler: () => list },
      {
        method: "POST",
        path: "/drawings",
        handler: ({ body }) => {
          const now = ctx.now();
          const d: Drawing = { ...(body as DrawingInput), id: `d-${now.toString(36)}-${seq++}`, createdTs: now, updatedTs: now };
          list = [...list, d];
          changed();
          return d;
        },
      },
      {
        method: "PUT",
        path: "/drawings",
        handler: ({ body }) => {
          list = (body as Drawing[]) ?? [];
          changed();
          return list;
        },
      },
      {
        method: "DELETE",
        path: "/drawings",
        handler: () => {
          const deleted = list.length;
          list = [];
          changed();
          return { deleted };
        },
      },
      {
        method: "PUT",
        path: "/drawings/:id",
        handler: ({ params, body }) => {
          const d = { ...find(params.id), ...(body as Partial<DrawingInput>), id: params.id, updatedTs: ctx.now() };
          list = list.map((x) => (x.id === d.id ? d : x));
          changed();
          return d;
        },
      },
      {
        method: "DELETE",
        path: "/drawings/:id",
        handler: ({ params }) => {
          find(params.id);
          list = list.filter((x) => x.id !== params.id);
          changed();
          return { ok: true };
        },
      },
    ],
    snapshot: () => ({ drawings: list }),
  };
}

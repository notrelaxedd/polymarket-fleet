// Dispatches api.ts REST calls to the mock modules' routes (mock mode only). OWNER: scaffold.
import { ApiError } from "../api-error";
import type { HttpMethod, MockModule, MockRequest, MockRoute } from "./types";

/** simulated network latency, ms (lets pending states show) */
export const MOCK_LATENCY_MS = 40;

interface Compiled {
  route: MockRoute;
  re: RegExp;
  keys: string[];
}

let table: Compiled[] = [];

export function setMockRoutes(modules: MockModule[]): void {
  table = [];
  for (const m of modules) {
    for (const route of m.routes ?? []) {
      const keys: string[] = [];
      const re = new RegExp(
        "^" +
          route.path.replace(/\/:([A-Za-z_]+)/g, (_s, k: string) => {
            keys.push(k);
            return "/([^/]+)";
          }) +
          "$",
      );
      table.push({ route, re, keys });
    }
  }
  // static segments before params ("/alerts/events" before "/alerts/:id")
  table.sort((a, b) => a.keys.length - b.keys.length);
}

const clone = <T>(v: T): T => (v === undefined ? v : (JSON.parse(JSON.stringify(v)) as T));

/** `path` is the api.ts path under /api, possibly with a query string. Resolves with a JSON-cloned result. */
export async function mockRequest<T>(method: HttpMethod, path: string, body?: unknown): Promise<T> {
  await new Promise((r) => setTimeout(r, MOCK_LATENCY_MS));
  const [pathname, qs = ""] = path.split("?");
  const query: Record<string, string> = {};
  new URLSearchParams(qs).forEach((v, k) => (query[k] = v));
  for (const c of table) {
    if (c.route.method !== method) continue;
    const m = c.re.exec(pathname);
    if (!m) continue;
    const params: Record<string, string> = {};
    c.keys.forEach((k, i) => (params[k] = decodeURIComponent(m[i + 1])));
    const req: MockRequest = { params, query, body: clone(body) };
    return clone((await c.route.handler(req)) as T);
  }
  throw new ApiError(404, "not_found", `mock: no route for ${method} ${pathname}`);
}

# Fleet control 3D (`/fleet`)

The owner's 3D control page for the fleet: React + TypeScript + Vite + three.js. The
host serves the built page at `/fleet` and its files under `/fleet/assets/`, on the same
origin as the API and behind the same Tailscale owner auth (the browser gets it for
free, so there is no login form). Nothing loads from a third party: the fonts are
bundled from `@fontsource`.

## How it works

- Polls `GET /api/fleet` every 3 s (and right after every action) and
  `GET /api/fleet/events?since=<newest ts seen>` every 3 s. The API is specified in
  `docs/PROTOCOL.md`, "Fleet UI additions".
- Role buttons and the command bar call `POST /api/workers/{id}/role` with
  `{"role": "<role id>"}`; Reboot calls `POST /api/workers/{id}/reboot`. Any change into
  or out of `trade` asks for confirmation first. Errors show the host's `detail`.
- The feed merges the host's events with ones the page derives by comparing polls
  ("Back online", "Went offline", "Passed 72 °C") and console feedback.
- The header chip reads Live while the last poll succeeded within 10 s. A 401 or 403
  shows "Not signed in" instead of the stage.

Files:

- `src/fleet.ts`: the node shape the scene reads, the role list, the API-to-node mapping.
- `src/api.ts`: the four API calls.
- `src/commands.ts`: the command bar's parser (`reboot 5`, `search on idle`, `stop search`,
  `box3 to trade`, `backtest box1 box2`).
- `src/App.tsx`, `src/Inspector.tsx`, `src/ui.ts`: the HUD (header, machine list,
  inspector, console, feed). Owns polling and actions.
- `src/Scene.tsx`: renderer, bloom, labels, controls; rebuilds the city when the worker
  ids change.
- `src/scene/`: the Data city (`city.ts`: one tower per worker on a grid that fits any
  number of workers; `cityDistrict.ts`: the surrounding streets, river and traffic) and
  `shared.ts`.
- `src/index.css`: all styling (design tokens at the top).

## Build

```sh
cd fleet-ui
npm install
npm run build     # tsc -b && vite build, output in fleet-ui/dist
```

The host serves `fleet-ui/dist` (`index.html` at `/fleet`, assets at `/fleet/assets/`).

## Develop

Start a local host with `FLEET_DEV=1` on port 8080, then:

```sh
npm run dev       # http://localhost:5173/fleet/ ; /api is proxied to http://127.0.0.1:8080
```

The role and reboot buttons post from Vite's origin, which the host's Origin check
refuses unless it is allowed, e.g.
`FLEET_ALLOWED_ORIGINS=http://127.0.0.1:8080,http://localhost:5173`.

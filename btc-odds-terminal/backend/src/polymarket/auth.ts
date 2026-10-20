// Ed25519 request signing for api.polymarket.us (trading REST and both WebSockets).
// Verified against the official SDK (polymarket-us-typescript src/auth.ts, src/websocket/base.ts):
//   message   = `${Date.now()}${METHOD}${path}`   (path = URL pathname, no query string, no body)
//   signature = base64(Ed25519(message)), secret = base64 32-byte seed (64-byte key -> first 32 bytes)
//   headers   = X-PM-Access-Key, X-PM-Timestamp, X-PM-Signature
// WebSocket: the same headers on the HTTP upgrade, signed as "GET" + the stream path.

import * as ed from "@noble/ed25519";

export interface PmAuthHeaders {
  "X-PM-Access-Key": string;
  "X-PM-Timestamp": string;
  "X-PM-Signature": string;
}

export function decodeSecret(secretB64: string): Uint8Array {
  const bytes = Uint8Array.from(Buffer.from(secretB64.trim(), "base64"));
  if (bytes.length === 64) return bytes.slice(0, 32);
  if (bytes.length !== 32) throw new Error(`Polymarket US secret must decode to 32 or 64 bytes (got ${bytes.length})`);
  return bytes;
}

export async function createAuthHeaders(
  keyId: string,
  secretB64: string,
  method: string,
  path: string,
  nowMs: number = Date.now(),
): Promise<PmAuthHeaders> {
  const timestamp = String(Math.floor(nowMs));
  const message = new TextEncoder().encode(`${timestamp}${method.toUpperCase()}${path}`);
  const sig = await ed.signAsync(message, decodeSecret(secretB64));
  return {
    "X-PM-Access-Key": keyId,
    "X-PM-Timestamp": timestamp,
    "X-PM-Signature": Buffer.from(sig).toString("base64"),
  };
}

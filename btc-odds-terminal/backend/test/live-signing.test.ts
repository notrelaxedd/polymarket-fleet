// Ed25519 request signing for live trading (docs/POLYMARKET_US.md "Signing"; SDK src/auth.ts).
// Known vectors: the SDK's own test secret (tests/auth.test.ts, orders.test.ts) is the RFC 8032 TEST 1 seed, so the
// RFC signature pins the primitive + secret decoding, and the X-PM-Signature vectors below were produced
// independently with node:crypto (OpenSSL Ed25519) for fixed timestamps.
import { createPrivateKey, createPublicKey, verify } from "node:crypto";
import * as ed from "@noble/ed25519";
import { describe, expect, it } from "vitest";
import { LiveRestClient } from "../src/live/rest.ts";
import { createAuthHeaders, decodeSecret } from "../src/polymarket/auth.ts";

/** SDK test secret (polymarket-us-typescript tests/auth.test.ts) = RFC 8032 7.1 TEST 1 secret key. */
const SDK_SECRET = "nWGxne/9WmC6hEr0kuwsxERJxWl7MmkZcDusAxyuf2A=";
const RFC1_SEED = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60";
const RFC1_PUB = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a";
const RFC1_SIG_EMPTY =
  "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b";

const TS = 1_700_000_000_000;
const VECTORS: [method: string, path: string, sig: string][] = [
  ["GET", "/v1/orders/open", "IRoBip2upDBZkMRT7PBTGAhNwKQLfM1TrvDDYxlSZMgtBsEvAYt7siWpbvMOGHDD1V0qH22t+G9g8MIfEyXIBA=="],
  ["POST", "/v1/orders", "l/ry9TVLSH7BiT8MhbMrgAvrZzz61esR9V567RoRuEXADHNFk1rza0I+erHgAdQjTsT5Hke29D+9cYizzg01DA=="],
  ["GET", "/v1/ws/private", "iRm0TQwqG76YaGtU18UQO81tLkw1Jyr1qqP9TjklrUuzlXe0+gQKlk/v/21RQVNUjARn4vdXy7IURJeSXeReDw=="],
];

const pubKey = createPublicKey(
  createPrivateKey({ key: Buffer.concat([Buffer.from("302e020100300506032b657004220420", "hex"), Buffer.from(RFC1_SEED, "hex")]), format: "der", type: "pkcs8" }),
);
const verifies = (message: string, sigB64: string) => verify(null, Buffer.from(message), pubKey, Buffer.from(sigB64, "base64"));

describe("live signing (Ed25519, X-PM-* headers)", () => {
  it("decodes the SDK test secret to the RFC 8032 TEST 1 seed and reproduces the RFC signature", async () => {
    const seed = decodeSecret(SDK_SECRET);
    expect(Buffer.from(seed).toString("hex")).toBe(RFC1_SEED);
    expect(Buffer.from(await ed.getPublicKeyAsync(seed)).toString("hex")).toBe(RFC1_PUB);
    expect(Buffer.from(await ed.signAsync(new Uint8Array(0), seed)).toString("hex")).toBe(RFC1_SIG_EMPTY);
  });

  it.each(VECTORS)("signs %s %s as timestampMs + METHOD + path (known vector)", async (method, path, sig) => {
    const h = await createAuthHeaders("test-key-id", SDK_SECRET, method, path, TS);
    expect(h).toEqual({ "X-PM-Access-Key": "test-key-id", "X-PM-Timestamp": String(TS), "X-PM-Signature": sig });
    expect(verifies(`${TS}${method}${path}`, h["X-PM-Signature"])).toBe(true);
  });

  it("uses the first 32 bytes of a 64-byte secret (seed || public key), like the SDK", async () => {
    const sixtyFour = Buffer.concat([Buffer.from(RFC1_SEED, "hex"), Buffer.from(RFC1_PUB, "hex")]).toString("base64");
    const h = await createAuthHeaders("k", sixtyFour, "POST", "/v1/orders", TS);
    expect(h["X-PM-Signature"]).toBe(VECTORS[1]![2]);
  });

  it("upper-cases the method and signs the pathname only (no query string, no body)", async () => {
    const calls: { url: string; headers: Record<string, string> }[] = [];
    const fetchImpl = (async (url: string, init: RequestInit) => {
      calls.push({ url, headers: init.headers as Record<string, string> });
      return new Response(JSON.stringify({ orders: [] }), { status: 200 });
    }) as unknown as typeof fetch;
    const rest = new LiveRestClient({ baseUrl: "https://api.polymarket.us", keyId: "kid", secretKey: SDK_SECRET, fetchImpl, now: () => TS });
    await rest.openOrders(["a", "b"]);
    expect(calls[0]!.url).toBe("https://api.polymarket.us/v1/orders/open?slugs=a&slugs=b");
    expect(calls[0]!.headers["X-PM-Signature"]).toBe(VECTORS[0]![2]);
    expect(calls[0]!.headers["X-PM-Timestamp"]).toBe(String(TS));
    expect(calls[0]!.headers["X-PM-Access-Key"]).toBe("kid");
  });

  it("rejects a malformed secret without echoing it", () => {
    const secret = Buffer.alloc(20, 7).toString("base64");
    let msg = "";
    try {
      new LiveRestClient({ baseUrl: "https://x", keyId: "k", secretKey: secret });
    } catch (err) {
      msg = (err as Error).message;
    }
    expect(msg).toMatch(/32 or 64 bytes/);
    expect(msg).not.toContain(secret);
  });
});

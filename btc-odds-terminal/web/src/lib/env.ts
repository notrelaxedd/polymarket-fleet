export const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8787").replace(/\/$/, "");
export const WS_URL = process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8787/ws";

/** Mock backend is used when NEXT_PUBLIC_MOCK=1 or the page URL has ?mock=1. */
export function isMock(): boolean {
  if (process.env.NEXT_PUBLIC_MOCK === "1") return true;
  if (typeof window === "undefined") return false;
  try {
    return new URLSearchParams(window.location.search).get("mock") === "1";
  } catch {
    return false;
  }
}

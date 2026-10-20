// Canvas images for the round SVG badge stickers (components/stickers.tsx). The sticker artwork is a tree
// of plain SVG React elements; it is serialized to standalone SVG markup once per kind and decoded into an
// HTMLImageElement that the drawing primitive paints with drawImage().

import * as React from "react";
import type { StickerKind } from "@shared/protocol";
import { STICKERS } from "@/components/stickers";

const KEEP_CAMEL = new Set(["viewBox", "preserveAspectRatio", "gradientUnits", "gradientTransform", "patternUnits"]);
const SKIP = new Set(["children", "key", "ref", "className", "style"]);

const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const attrName = (k: string) => (KEEP_CAMEL.has(k) ? k : k.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`));

/** Serialize a tree of intrinsic SVG React elements (fragments and arrays allowed) to markup. */
export function svgMarkup(node: React.ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return esc(String(node));
  if (Array.isArray(node)) return node.map(svgMarkup).join("");
  if (!React.isValidElement(node)) return "";
  const el = node as React.ReactElement<Record<string, unknown>>;
  const props = el.props ?? {};
  if (el.type === React.Fragment) return svgMarkup(props.children as React.ReactNode);
  if (typeof el.type !== "string") return ""; // components are not expected inside sticker glyphs
  let attrs = "";
  for (const [k, v] of Object.entries(props)) {
    if (SKIP.has(k) || v == null || typeof v === "function" || typeof v === "object" || v === false) continue;
    attrs += ` ${attrName(k)}="${esc(String(v))}"`;
  }
  return `<${el.type}${attrs}>${svgMarkup(props.children as React.ReactNode)}</${el.type}>`;
}

/** Standalone SVG document for one sticker, same artwork as <Sticker/>. */
export function stickerSvg(kind: StickerKind, size = 64): string {
  const d = STICKERS[kind];
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 24 24">` +
    `<circle cx="12" cy="12" r="11.2" fill="${d.color}" stroke="rgba(255,255,255,0.28)" stroke-width="0.8"/>` +
    svgMarkup(d.glyph) +
    `</svg>`
  );
}

const images = new Map<StickerKind, HTMLImageElement>();
const waiting = new Set<() => void>();

/**
 * Decoded image for a sticker, or null while it is still loading (`onReady` is called once it is,
 * so the caller can repaint).
 */
export function stickerImage(kind: StickerKind, onReady?: () => void): HTMLImageElement | null {
  if (typeof Image === "undefined" || !STICKERS[kind]) return null;
  let img = images.get(kind);
  if (!img) {
    img = new Image();
    img.decoding = "async";
    img.onload = () => {
      const fs = [...waiting];
      waiting.clear();
      for (const f of fs) f();
    };
    img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(stickerSvg(kind))}`;
    images.set(kind, img);
  }
  if (img.complete && img.naturalWidth > 0) return img;
  if (onReady) waiting.add(onReady);
  return null;
}

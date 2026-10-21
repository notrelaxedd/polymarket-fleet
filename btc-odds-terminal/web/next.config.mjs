/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // NEXT_DIST_DIR lets several dev servers/builds run side by side (e.g. parallel agents).
  distDir: process.env.NEXT_DIST_DIR || ".next",
  experimental: { externalDir: true },
};
export default nextConfig;

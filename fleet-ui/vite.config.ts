import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// Served by the host at /fleet (assets under /fleet/assets/). `npm run dev` proxies the
// API to a local host started with FLEET_DEV=1 on port 8080.
export default defineConfig({
  base: '/fleet/',
  plugins: [react()],
  build: {
    outDir: 'dist',
    sourcemap: false,
    // three.js alone is ~600 kB; one chunk is fine for a page served on the tailnet
    chunkSizeWarningLimit: 1200,
  },
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8080', changeOrigin: false },
    },
  },
});

import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';
// Vitest's `defineConfig` rather than Vite's: the `test` block below is not part
// of Vite's own config type, and Vite's overload rejects it.
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5173,
    // The refresh token is an HttpOnly cookie, so the dev server must proxy the
    // API rather than the browser calling it cross-origin - otherwise the cookie
    // is never sent and every reload logs the user out.
    proxy: {
      // `VITE_PROXY_TARGET` so the dev server works both on the host (default) and
      // inside a container, where `localhost` is the container itself and the
      // backend is reachable by its compose service name.
      '/api': {
        target: process.env.VITE_PROXY_TARGET ?? 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    // The default 500 kB warning exists to catch a bloated *critical path*. Routes
    // are lazily loaded (see `App.tsx`), so the only chunks above the default are
    // `pdf` (~364 kB) and `charts` (~512 kB), each fetched only when the user opens
    // the contract viewer or the dashboard. The entry chunk is ~107 kB, and the
    // limit is set just above the largest vendor chunk so that a *third* large
    // chunk - or either of these growing - still trips the warning.
    chunkSizeWarningLimit: 520,
    rollupOptions: {
      output: {
        manualChunks: {
          // Held in their own chunks rather than folded into the route chunk that
          // pulls them: neither library changes when application code does, so a
          // deploy does not invalidate 900 kB of cached vendor bundle.
          pdf: ['pdfjs-dist'],
          charts: ['recharts'],
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
  },
});

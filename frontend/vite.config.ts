import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';
// Vitest's `defineConfig` rather than Vite's: the `test` block below is not part
// of Vite's own config type, and Vite's overload rejects it.
import { defineConfig, loadEnv } from 'vitest/config';

export default defineConfig(({ mode }) => {
  // `.env` files are loaded *after* the config is evaluated, and Vite never copies
  // them into `process.env`, so reading them here takes an explicit `loadEnv`. The
  // empty prefix loads every key rather than only `VITE_*`: `VITE_PROXY_TARGET` is
  // consumed by the dev server below, not by the browser, so it is never bundled.
  const env = loadEnv(mode, fileURLToPath(new URL('.', import.meta.url)), '');

  return {
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
        // `VITE_PROXY_TARGET` so the dev server works against a local backend
        // (default), a container, where `localhost` is the container itself and the
        // backend is reachable by its compose service name, or a remote deployment.
        //
        // `||` rather than `??`: `loadEnv` yields '' for an absent key, and an empty
        // target reaches the proxy as a malformed URL instead of falling back.
        '/api': {
          target: env.VITE_PROXY_TARGET || 'http://localhost:8000',
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
  };
});

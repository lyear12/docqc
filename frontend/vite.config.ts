import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

/**
 * Vite configuration.
 *
 * In development the UI is served by Vite on 5173 and the API by uvicorn on
 * 8765, so every API call is cross-origin. Two ways to handle that: enable CORS
 * on the server and put the API's absolute URL in the client, or proxy. The
 * proxy is better here because it makes development and production use the
 * *same* relative URLs — `/api/jobs` means the same thing in both, so a
 * cross-origin bug cannot hide until deploy.
 *
 * The server also allows localhost CORS (see `docqc/web/app.py`), which keeps
 * `VITE_API_BASE` working as an escape hatch when running the API elsewhere.
 *
 * `/api/jobs/*\/events` is SSE: the proxy must not buffer, or progress arrives
 * in one burst at the end instead of streaming.
 */
export default defineConfig({
  plugins: [react()],

  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
        // Required for Server-Sent Events: without it Vite buffers the response
        // and the progress stream appears only when the run finishes.
        ws: false,
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            if (proxyRes.headers['content-type']?.includes('text/event-stream')) {
              // Disable any intermediate buffering for this response.
              proxyRes.headers['x-accel-buffering'] = 'no'
              proxyRes.headers['cache-control'] = 'no-cache'
            }
          })
        },
      },
    },
  },

  build: {
    // The FastAPI app serves `frontend/dist` at `/` in production, so asset
    // URLs must be relative to the mount point rather than the site root.
    outDir: 'dist',
    sourcemap: true,
  },
})

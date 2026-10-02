import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// The bundle is committed under ../static (the control plane serves it at /ui, and the Python
// tests and pip installs need no Node). File names are fixed so a rebuild only changes files
// whose content changed; CI rebuilds and fails on any difference.
export default defineConfig({
  base: '/ui/',
  plugins: [react()],
  build: {
    outDir: '../static',
    emptyOutDir: true,
    sourcemap: false,
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        entryFileNames: 'assets/app.js',
        chunkFileNames: 'assets/[name].js',
        assetFileNames: 'assets/[name][extname]',
      },
    },
  },
  server: {
    // `npm run dev` proxies the API to a running control plane (make cp-demo).
    proxy: { '^/(projects|pipeline-runs|runs|model-versions|rollouts)': 'http://127.0.0.1:8080' },
  },
  test: { environment: 'node' },
});

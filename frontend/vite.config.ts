import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    port: 5173,
    // `npm run dev` talks to a backend running on :8000; in production the
    // backend serves this build itself, so there is only ever one origin.
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
})

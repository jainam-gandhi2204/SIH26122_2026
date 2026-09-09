import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [
    tailwindcss(),
    react(),
  ],
  server: {
    proxy: {
      '/schedule': 'http://127.0.0.1:8000',
      '/site-updates': 'http://127.0.0.1:8000',
      '/planner': 'http://127.0.0.1:8000',
      '/review': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
    },
  },
})

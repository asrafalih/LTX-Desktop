import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import electron from 'vite-plugin-electron'
import renderer from 'vite-plugin-electron-renderer'
import path from 'path'

// Parallel checkout beside another LTX Desktop: LTX_PARALLEL_DEV=1 (or pnpm dev:parallel).
// Defaults: Vite 5173, backend 41954, app folder LTXDesktop.
if (process.env.LTX_PARALLEL_DEV === '1') {
  process.env.LTX_VITE_PORT ||= '5174'
  process.env.LTX_PORT ||= '41955'
  process.env.LTX_APP_FOLDER_NAME ||= 'LTXDesktop-Dev'
  process.env.LTX_ALLOW_MULTI_INSTANCE ||= '1'
}

const vitePort = Number(process.env.LTX_VITE_PORT || 5173)

export default defineConfig({
  plugins: [
    react(),
    electron([
      {
        entry: 'electron/main.ts',
        onstart(options) {
          if (process.env.ELECTRON_DEBUG) {
            // --inspect and --remote-debugging-port must come before '.' (the app path)
            options.startup(['--inspect=9229', '--remote-debugging-port=9222', '.', '--no-sandbox'])
          } else {
            options.startup()
          }
        },
        vite: {
          build: {
            outDir: 'dist-electron',
            sourcemap: true,
            rollupOptions: {
              external: ['electron', 'koffi']
            }
          }
        }
      },
      {
        entry: 'electron/preload.ts',
        onstart(options) {
          options.reload()
        },
        vite: {
          build: {
            outDir: 'dist-electron',
            sourcemap: true,
            rollupOptions: {
              output: {
                format: 'cjs'  // Preload must be CommonJS
              }
            }
          }
        }
      }
    ]),
    renderer()
  ],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './frontend')
    }
  },
  base: './',  // Use relative paths for Electron file:// protocol
  server: {
    port: vitePort,
    strictPort: true,
  },
  build: {
    outDir: 'dist'
  }
})

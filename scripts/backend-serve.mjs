#!/usr/bin/env node
/**
 * backend:serve — run the FastAPI backend headless (no Electron).
 *
 * Reuses the desktop app data dir (models/settings) unless LTX_APP_DATA_DIR is set.
 * Set LTX_API_TOKEN for scripts/LAN. Set LTX_BIND_HOST=0.0.0.0 for LAN (requires a token).
 *
 *   LTX_API_TOKEN=… pnpm backend:serve
 *   LTX_API_TOKEN=… LTX_BIND_HOST=0.0.0.0 pnpm backend:serve
 */
import { spawn, spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const BACKEND_DIR = path.join(ROOT, 'backend')
const APP_FOLDER_NAME = 'LTXDesktop'
const PORT = process.env.LTX_PORT || '41954'
const BIND = process.env.LTX_BIND_HOST || '127.0.0.1'

function appDataDir() {
  if (process.env.LTX_APP_DATA_DIR) return process.env.LTX_APP_DATA_DIR
  if (process.platform === 'win32') {
    const base = process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local')
    return path.join(base, APP_FOLDER_NAME)
  }
  if (process.platform === 'darwin') {
    return path.join(os.homedir(), 'Library', 'Application Support', APP_FOLDER_NAME)
  }
  const xdg = process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share')
  return path.join(xdg, APP_FOLDER_NAME)
}

const HAS_UV = spawnSync('uv', ['--version'], { stdio: 'ignore' }).status === 0
const appData = appDataDir()
const token = process.env.LTX_API_TOKEN || process.env.LTX_AUTH_TOKEN || ''

console.log('\nbackend:serve — headless FastAPI')
console.log(`  app data : ${appData}`)
console.log(`  listen   : http://${BIND}:${PORT}`)
console.log(`  docs     : http://127.0.0.1:${PORT}/docs  (also via LAN IP when bind is 0.0.0.0)`)
console.log(`  token    : ${token ? 'set (LTX_API_TOKEN / LTX_AUTH_TOKEN)' : 'NOT SET — /api requires a token when configured; LAN bind will refuse to start'}`)
console.log('  Do not run this while the desktop app backend is already on the same port.')
console.log('  Ctrl+C to stop.\n')

const args = HAS_UV ? ['run', 'python', 'ltx2_server.py'] : ['ltx2_server.py']
const cmd = HAS_UV ? 'uv' : 'python'
const child = spawn(cmd, args, {
  cwd: BACKEND_DIR,
  env: {
    ...process.env,
    LTX_APP_DATA_DIR: appData,
    PYTHONUNBUFFERED: '1',
    LTX_DEV_MODE: process.env.LTX_DEV_MODE || '1',
    PYTORCH_ENABLE_MPS_FALLBACK: '1',
  },
  stdio: 'inherit',
})

child.on('exit', (code) => process.exit(code ?? 1))
process.on('SIGINT', () => child.kill('SIGINT'))
process.on('SIGTERM', () => child.kill('SIGTERM'))

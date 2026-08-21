import path from 'path'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    include: ['frontend/**/*.test.ts'],
    environment: 'node',
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'frontend'),
    },
  },
})

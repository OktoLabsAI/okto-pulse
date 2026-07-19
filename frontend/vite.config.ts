/// <reference types="vitest" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'
import { readFileSync } from 'node:fs'

const communityPyproject = readFileSync(
  path.resolve(__dirname, '../pyproject.toml'),
  'utf8',
)
const communityVersionMatch = communityPyproject.match(
  /^version\s*=\s*["']([^"']+)["']/m,
)
if (!communityVersionMatch) {
  throw new Error('Unable to resolve the Community release version from pyproject.toml')
}
const communityVersion = communityVersionMatch[1]

export default defineConfig({
  base: process.env.VITE_BASE_PATH || '/',
  define: {
    __AUTH_MODE__: JSON.stringify(process.env.VITE_AUTH_MODE || 'local'),
    __APP_VERSION__: JSON.stringify(communityVersion),
  },
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5174,
    proxy: {
      '/api': {
        target: 'http://localhost:8100',
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    include: ['src/**/*.{test,spec}.{ts,tsx}', 'tests/unit/**/*.{test,spec}.ts'],
    exclude: ['tests/e2e/**', 'node_modules/**'],
  },
})

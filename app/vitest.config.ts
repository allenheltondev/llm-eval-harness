import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./vitest.setup.ts'],
    css: true,
    exclude: ['node_modules/**', 'dist/**'],
    coverage: {
      provider: 'v8',
      include: ['src/**'],
      exclude: [
        'src/**/__tests__/**',
        'src/**/*.d.ts',
        'e2e/**',
        // Bootstrap only: mounts React, no branching logic of its own to cover.
        'src/main.tsx'
      ],
      // Ratchet, not aspiration: pinned at the achieved numbers (see the
      // coverage-quality pass notes), rounded down to whole percents. Bump
      // these up when coverage improves; never down without a documented
      // reason (an excluded file, a removed test).
      thresholds: {
        statements: 98,
        branches: 91,
        functions: 95,
        lines: 98
      }
    }
  }
})

import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'node',
    include: ['tests/**/*.test.ts'],
    env: {
      TABLE_NAME: 'llm-eval-harness-table',
      ORIGIN: '*',
      POWERTOOLS_SERVICE_NAME: 'llm-eval-harness-test',
      POWERTOOLS_LOG_LEVEL: 'ERROR',
    },
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html', 'lcov', 'json-summary'],
      include: ['functions/**', 'seed/lib/**'],
      exclude: [
        'tests/**',
        '**/*.d.mts',
        '**/*.d.ts',
        'functions/common/types.ts',
      ],
      thresholds: {
        statements: 100,
        branches: 98,
        functions: 100,
        lines: 100,
      },
    },
  },
});

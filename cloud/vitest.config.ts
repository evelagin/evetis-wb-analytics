import { defineConfig } from 'vitest/config';

export default defineConfig({
  // Позволяет ESM-импортам вида '../src/x.js' резолвиться в x.ts (как в tsc bundler).
  resolve: { extensionAlias: { '.js': ['.ts', '.js'] } },
  test: { include: ['test/**/*.test.ts'], environment: 'node' },
});

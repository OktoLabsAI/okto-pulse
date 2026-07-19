import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { readdir, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { describe, expect, it } from 'vitest';

const SECRET_RE = /dash_[A-Za-z0-9_.-]+/g;
const SCAN_DIRS = ['src', 'tests'];
const EXCLUDED_FILES = new Set(['tests/unit/frontendCredentialSurfaceGate.test.ts']);

const ALLOWED_BY_FILE: Record<string, Set<string>> = {
  'src/components/help/HelpPanel.tsx': new Set(['dash_your_key_here']),
  'src/components/onboarding/AssistantBindingSlide.tsx': new Set(['dash_...']),
  'src/components/onboarding/__tests__/OnboardingModal.test.tsx': new Set(['dash_...']),
  'src/components/layout/AgentsModal.reveal-once.test.tsx': new Set([
    'dash_new_secret',
    'dash_rotated_secret',
  ]),
};

interface Finding {
  file: string;
  value: string;
}

async function collectFiles(dir: string): Promise<string[]> {
  let entries;
  try {
    entries = await readdir(dir, { withFileTypes: true });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return [];
    throw error;
  }
  const files = await Promise.all(
    entries.map(async (entry) => {
      const fullPath = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (
          entry.name === 'node_modules' ||
          entry.name === 'dist' ||
          entry.name === 'test-results' ||
          entry.name === 'screenshots'
        ) {
          return [];
        }
        return collectFiles(fullPath);
      }
      if (!/\.(ts|tsx|js|jsx)$/.test(entry.name)) return [];
      return [fullPath];
    }),
  );
  return files.flat();
}

async function runFrontendCredentialSurfaceGate(root = process.cwd()): Promise<Finding[]> {
  const allFiles = (
    await Promise.all(SCAN_DIRS.map((dir) => collectFiles(path.join(root, dir))))
  ).flat();
  const findings: Finding[] = [];

  for (const filePath of allFiles) {
    const rel = path.relative(root, filePath).replaceAll(path.sep, '/');
    if (EXCLUDED_FILES.has(rel)) continue;

    const text = await readFile(filePath, 'utf8');
    const allowed = ALLOWED_BY_FILE[rel] ?? new Set<string>();
    const matches = text.matchAll(SECRET_RE);

    for (const match of matches) {
      const value = match[0];
      if (!allowed.has(value)) {
        findings.push({ file: rel, value });
      }
    }
  }

  return findings;
}

describe('frontend credential surface gate', () => {
  it('allows only reveal-once test sentinels and exact documentation placeholders', { timeout: 30_000 }, async () => {
    await expect(runFrontendCredentialSurfaceGate()).resolves.toEqual([]);
  });

  it('flags a secret sentinel outside the reveal-once allowlist', async () => {
    const root = mkdtempSync(path.join(tmpdir(), 'pulse-frontend-credential-gate-'));
    try {
      const rogue = path.join(root, 'src', 'components', 'Rogue.tsx');
      mkdirSync(path.dirname(rogue), { recursive: true });
      writeFileSync(
        rogue,
        `export function Rogue() { return <div>{'${'dash' + '_rogue_secret'}'}</div>; }`,
        'utf8',
      );

      await expect(runFrontendCredentialSurfaceGate(root)).resolves.toEqual([
        { file: 'src/components/Rogue.tsx', value: 'dash_rogue_secret' },
      ]);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

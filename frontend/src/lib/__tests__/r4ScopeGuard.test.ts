/**
 * R4 AC7 (ts_65314f5c) — scope guard: the R4 surfacing/freshness work is confined
 * to the frontend; ZERO changes to the okto_labs_pulse_core backend (a SEPARATE
 * git repo). The backend already delivers 400+detail (out of scope), so this spec
 * must touch only `okto_labs_pulse_community/frontend`.
 *
 * The `then` is "all modified files are under okto_labs_pulse_community/frontend;
 * no okto_labs_pulse_core file was changed for R4". We prove it from the working
 * tree of BOTH repos rather than trusting a reviewer's private inspection.
 */
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';

import { describe, expect, it } from 'vitest';

const CORE = 'D:/Projetos/Techridy/okto_labs_pulse_core';
const COMMUNITY = 'D:/Projetos/Techridy/okto_labs_pulse_community';

// Symbols introduced/used by R4. None of them may appear in any modified core file.
const R4_SYMBOLS = ['getErrorMessage', 'addBoard(', "from '@/lib/getErrorMessage'"];
const SCOPE_GUARD_TIMEOUT_MS = 20_000;

function changedFiles(repo: string): string[] {
  const out = execFileSync('git', ['-C', repo, 'status', '--porcelain'], {
    encoding: 'utf8',
  });
  return out
    .split('\n')
    .map((line) => {
      const file = line.slice(3).trim();
      return file.includes(' -> ') ? file.split(' -> ').at(-1) ?? '' : file;
    })
    .filter(Boolean);
}

function fileText(repo: string, file: string): string {
  const absolute = `${repo}/${file}`;
  return existsSync(absolute) ? readFileSync(absolute, 'utf8') : '';
}

function filesWithR4Symbols(repo: string, files: string[]): string[] {
  const offenders: string[] = [];
  for (const f of files) {
    if (!/\.(py|ts|tsx|md)$/.test(f)) continue;
    const text = fileText(repo, f);
    if (R4_SYMBOLS.some((s) => text.includes(s))) offenders.push(f);
  }
  return offenders;
}

describe('R4 AC7 — scope guard (zero okto_labs_pulse_core changes)', () => {
  it(
    'no changed okto_labs_pulse_core file carries an R4 symbol',
    () => {
      expect(filesWithR4Symbols(CORE, changedFiles(CORE))).toEqual([]);
    },
    SCOPE_GUARD_TIMEOUT_MS,
  );

  it(
    'within the community repo, backend python changes do not carry R4 symbols',
    () => {
      // The community repo's own python backend lives at src/okto_pulse; other
      // specs may touch it in the same release worktree, but R4 symbols may not.
      const backendFiles = changedFiles(COMMUNITY).filter(
        (f) => f.startsWith('src/okto_pulse'),
      );
      expect(filesWithR4Symbols(COMMUNITY, backendFiles)).toEqual([]);
    },
    SCOPE_GUARD_TIMEOUT_MS,
  );
});

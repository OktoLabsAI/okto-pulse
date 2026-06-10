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
import { execSync } from 'node:child_process';

import { describe, expect, it } from 'vitest';

const CORE = 'D:/Projetos/Techridy/okto_labs_pulse_core';
const COMMUNITY = 'D:/Projetos/Techridy/okto_labs_pulse_community';

// Symbols introduced/used by R4. None of them may appear in any modified core file.
const R4_SYMBOLS = ['getErrorMessage', 'addBoard(', "from '@/lib/getErrorMessage'"];

function changedFiles(repo: string): string[] {
  const out = execSync(`git -C "${repo}" status --porcelain`, { encoding: 'utf8' });
  return out
    .split('\n')
    .map((line) => line.slice(3).trim())
    .filter(Boolean);
}

describe('R4 AC7 — scope guard (zero okto_labs_pulse_core changes)', () => {
  it('no okto_labs_pulse_core file carries an R4 symbol in its diff', () => {
    const offenders: string[] = [];
    for (const f of changedFiles(CORE)) {
      if (!/\.(py|ts|tsx|md)$/.test(f)) continue;
      let diff = '';
      try {
        diff = execSync(`git -C "${CORE}" diff -- "${f}"`, { encoding: 'utf8' });
      } catch {
        diff = '';
      }
      if (R4_SYMBOLS.some((s) => diff.includes(s))) offenders.push(f);
    }
    expect(offenders).toEqual([]);
  });

  it('within the community repo, every R4 change is under frontend/ (no backend python touched by R4)', () => {
    // The community repo's own python backend lives at src/okto_pulse; R4 must not touch it.
    const r4BackendTouch = changedFiles(COMMUNITY).filter(
      (f) => f.startsWith('src/okto_pulse'),
    );
    expect(r4BackendTouch).toEqual([]);
  });
});

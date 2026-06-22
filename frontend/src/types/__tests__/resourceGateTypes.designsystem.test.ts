// Spec 3a006f65 / card 390ccd50 / AC10 — Design System must NOT appear as a Resource
// Gate resource type in the frontend typing either. ResourceGateResourceType and the
// spec-resource auto-derive type are pure TS aliases (no runtime value), so this guard
// scans the source and FAILS if design_system is ever added as a Resource Gate type.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const TYPES = resolve(process.cwd(), 'src/types/index.ts');

function literalUnion(source: string, alias: string): string[] {
  const m = source.match(new RegExp(`export type ${alias}\\s*=\\s*([^;]+);`));
  if (!m) throw new Error(`type ${alias} not found`);
  return [...m[1].matchAll(/'([^']+)'/g)].map((x) => x[1]).sort();
}

describe('Resource Gate frontend typing excludes Design System', () => {
  const src = readFileSync(TYPES, 'utf-8');
  const canonical = ['architecture', 'knowledge_base', 'mockup'];

  it('ResourceGateResourceType is exactly the 3 canonical types', () => {
    expect(literalUnion(src, 'ResourceGateResourceType')).toEqual(canonical);
  });

  it('SpecResourceAutoDeriveType is exactly the 3 canonical types', () => {
    expect(literalUnion(src, 'SpecResourceAutoDeriveType')).toEqual(canonical);
  });

  it('neither Resource Gate type alias lists design_system', () => {
    expect(literalUnion(src, 'ResourceGateResourceType')).not.toContain('design_system');
    expect(literalUnion(src, 'SpecResourceAutoDeriveType')).not.toContain('design_system');
  });
});

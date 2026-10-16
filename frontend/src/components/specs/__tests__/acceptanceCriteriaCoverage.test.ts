import { describe, expect, it } from 'vitest';
import {
  getAcceptanceCriterionLabel,
  isAcceptanceCriterionLinked,
  normalizeAcceptanceCriteria,
} from '../acceptanceCriteriaCoverage';

describe('acceptance criteria coverage helpers', () => {
  it('uses the stable AC id for coverage and keeps the human-readable text', () => {
    const [criterion] = normalizeAcceptanceCriteria([
      { id: 'ac_83ada22a', text: 'Envelope paginado ativa por offset/limit' },
    ]);

    expect(criterion).toMatchObject({
      key: 'ac_83ada22a',
      reference: 'ac_83ada22a',
      label: 'Envelope paginado ativa por offset/limit',
    });
    expect(isAcceptanceCriterionLinked(['ac_83ada22a'], criterion, [criterion])).toBe(true);
  });

  it('resolves stable ids to AC text for scenario details', () => {
    const criteria = normalizeAcceptanceCriteria([
      { id: 'ac_83ada22a', text: 'Envelope paginado ativa por offset/limit' },
    ]);

    expect(getAcceptanceCriterionLabel('ac_83ada22a', criteria)).toBe(
      'Envelope paginado ativa por offset/limit',
    );
    expect(getAcceptanceCriterionLabel('ac_unknown', criteria)).toBe('ac_unknown');
  });

  it('refuses old criterion shapes without inventing identity', () => {
    for (const value of ['Old text', { title: 'Old title' }, { text: 'Missing id' }]) {
      expect(() => normalizeAcceptanceCriteria([value])).toThrow('incompatible_spec_requirement');
    }
  });

  it('does not resolve positions, text or prefixes as references', () => {
    const [criterion] = normalizeAcceptanceCriteria([{ id: 'ac_id', text: 'Current criterion' }]);
    for (const value of [0, '0', 'Current criterion', 'Current', true]) {
      expect(isAcceptanceCriterionLinked([value], criterion, [criterion])).toBe(false);
    }
  });

  it('does not collapse duplicate AC text or count orphan links', () => {
    const [first, second] = normalizeAcceptanceCriteria([
      { id: 'ac_first', text: 'Duplicate text' },
      { id: 'ac_second', text: 'Duplicate text' },
    ]);

    expect(isAcceptanceCriterionLinked(['Duplicate text'], first, [first, second])).toBe(false);
    expect(isAcceptanceCriterionLinked(['ac_second'], second, [first, second])).toBe(true);
    expect(isAcceptanceCriterionLinked(['Duplicate text'], second, [first, second])).toBe(false);
    expect(isAcceptanceCriterionLinked(['ac_missing'], first, [first, second])).toBe(false);
  });
});

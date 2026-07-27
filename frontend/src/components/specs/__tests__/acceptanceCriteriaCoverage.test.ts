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

  it('preserves legacy links by AC text and zero-based index', () => {
    const [first, second] = normalizeAcceptanceCriteria([
      'First legacy criterion',
      { title: 'Second legacy criterion' },
    ]);

    expect(isAcceptanceCriterionLinked(['First legacy criterion'], first, [first, second])).toBe(true);
    expect(isAcceptanceCriterionLinked(['1'], second, [first, second])).toBe(true);
    expect(getAcceptanceCriterionLabel('1', [first, second])).toBe('Second legacy criterion');
  });

  it('does not collapse duplicate AC text or count orphan links', () => {
    const [first, second] = normalizeAcceptanceCriteria([
      { id: 'ac_first', text: 'Duplicate text' },
      { id: 'ac_second', text: 'Duplicate text' },
    ]);

    expect(isAcceptanceCriterionLinked(['Duplicate text'], first, [first, second])).toBe(true);
    expect(isAcceptanceCriterionLinked(['Duplicate text'], second, [first, second])).toBe(false);
    expect(isAcceptanceCriterionLinked(['ac_missing'], first, [first, second])).toBe(false);
  });
});

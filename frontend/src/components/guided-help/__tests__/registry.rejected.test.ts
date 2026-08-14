import { describe, expect, it } from 'vitest';
import { guidedHelpRegistry } from '../registry';

describe('tasks Rejected guided help', () => {
  it('introduces the rework queue after the validation step', () => {
    const tour = guidedHelpRegistry.tours.find(({ id }) => id === 'tasks.workflow');
    expect(tour?.version).toBe('2');
    expect(tour?.steps.map(({ anchor }) => anchor)).toEqual([
      'tasks.validation.column',
      'tasks.rejected.column',
    ]);
    expect(tour?.steps[1].body).toMatch(/Test Cards never enter this column/i);
  });
});

import { describe, it, expect } from 'vitest';
import { driftIconFor, DRIFT_GOOD_THRESHOLD, DRIFT_BAD_THRESHOLD } from './driftIcon';

describe('driftIconFor', () => {
  it('retorna null para valores nulos/undefined/NaN', () => {
    expect(driftIconFor(null)).toBeNull();
    expect(driftIconFor(undefined)).toBeNull();
    expect(driftIconFor(NaN)).toBeNull();
  });

  it('drift < 10 → verde + down + reduziu', () => {
    const v = driftIconFor(5);
    expect(v).not.toBeNull();
    expect(v!.direction).toBe('down');
    expect(v!.color).toBe('green');
    expect(v!.label).toBe('reduziu');
  });

  it('drift = 0 → verde + down + reduziu', () => {
    const v = driftIconFor(0);
    expect(v!.color).toBe('green');
  });

  it('drift = 10 (threshold) → cinza + neutral + estável', () => {
    const v = driftIconFor(DRIFT_GOOD_THRESHOLD);
    expect(v!.direction).toBe('neutral');
    expect(v!.color).toBe('gray');
    expect(v!.label).toBe('estável');
  });

  it('drift = 20 → cinza + neutral + estável', () => {
    const v = driftIconFor(20);
    expect(v!.color).toBe('gray');
  });

  it('drift = 30 (threshold top) → cinza + neutral', () => {
    const v = driftIconFor(DRIFT_BAD_THRESHOLD);
    expect(v!.color).toBe('gray');
  });

  it('drift > 30 → vermelho + up + aumentou', () => {
    const v = driftIconFor(45);
    expect(v!.direction).toBe('up');
    expect(v!.color).toBe('red');
    expect(v!.label).toBe('aumentou');
  });

  it('nenhum estado combina vermelho+down (contra-exemplo do bug)', () => {
    const values = [0, 5, 10, 20, 30, 45, 99];
    for (const v of values) {
      const visual = driftIconFor(v);
      expect(visual).not.toBeNull();
      expect(visual!.color === 'red' && visual!.direction === 'down').toBe(false);
    }
  });
});

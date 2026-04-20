import { ArrowDown, ArrowUp, Minus } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

export const DRIFT_GOOD_THRESHOLD = 10;
export const DRIFT_BAD_THRESHOLD = 30;

export type DriftDirection = 'down' | 'neutral' | 'up';
export type DriftColor = 'green' | 'gray' | 'red';
export type DriftLabel = 'reduziu' | 'estável' | 'aumentou';

export interface DriftVisual {
  direction: DriftDirection;
  color: DriftColor;
  label: DriftLabel;
  icon: LucideIcon;
  colorClass: string;
}

export function driftIconFor(value: number | null | undefined): DriftVisual | null {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return null;
  }
  if (value < DRIFT_GOOD_THRESHOLD) {
    return {
      direction: 'down',
      color: 'green',
      label: 'reduziu',
      icon: ArrowDown,
      colorClass: 'text-green-500 dark:text-green-400',
    };
  }
  if (value <= DRIFT_BAD_THRESHOLD) {
    return {
      direction: 'neutral',
      color: 'gray',
      label: 'estável',
      icon: Minus,
      colorClass: 'text-gray-400 dark:text-gray-500',
    };
  }
  return {
    direction: 'up',
    color: 'red',
    label: 'aumentou',
    icon: ArrowUp,
    colorClass: 'text-red-500 dark:text-red-400',
  };
}

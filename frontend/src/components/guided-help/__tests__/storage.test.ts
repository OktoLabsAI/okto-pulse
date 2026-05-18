import { beforeEach, describe, expect, it } from 'vitest';

import {
  createGuidedHelpStorage,
  GUIDED_HELP_STORAGE_KEY,
  resolveFirstEligibleStep,
} from '../storage';
import type { GuidedHelpTour } from '../types';

const boardTour: GuidedHelpTour = {
  id: 'board.overview',
  title: 'Board overview',
  surface: 'board',
  version: '1',
  steps: [
    {
      id: 'board.step.one',
      title: 'First board step',
      body: 'First',
      anchor: 'board.one',
      kind: 'navigation',
      order: 10,
    },
    {
      id: 'board.step.two',
      title: 'Second board step',
      body: 'Second',
      anchor: 'board.two',
      kind: 'feature',
      order: 20,
    },
  ],
};

const metricsTour: GuidedHelpTour = {
  id: 'metrics.overview',
  title: 'Metrics overview',
  surface: 'metrics',
  version: '1',
  steps: [
    {
      id: 'metrics.step.one',
      title: 'Metrics step',
      body: 'Metrics',
      anchor: 'metrics.one',
      kind: 'feature',
      order: 10,
    },
  ],
};

beforeEach(() => {
  localStorage.clear();
});

describe('guided help storage', () => {
  it('tolerates invalid JSON and rewrites valid local-first progress', () => {
    localStorage.setItem(GUIDED_HELP_STORAGE_KEY, '{not-json');

    const storage = createGuidedHelpStorage();
    expect(storage.read().tours).toEqual({});

    const next = storage.markStepViewed(boardTour, boardTour.steps[0]);

    expect(next.tours[boardTour.id].steps['board.step.one'].status).toBe('viewed');
    expect(JSON.parse(localStorage.getItem(GUIDED_HELP_STORAGE_KEY) ?? '{}')).toMatchObject({
      schemaVersion: 1,
      skippedAll: false,
      tours: {
        [boardTour.id]: {
          version: '1',
          steps: {
            'board.step.one': {
              version: '1',
              status: 'viewed',
            },
          },
        },
      },
    });
  });

  it('falls back to in-memory state when storage is unavailable', () => {
    const brokenStorage = {
      getItem: (_key: string) => {
        throw new Error('blocked');
      },
      setItem: (_key: string, _value: string) => {
        throw new Error('blocked');
      },
      removeItem: (_key: string) => {
        throw new Error('blocked');
      },
    };

    const storage = createGuidedHelpStorage(brokenStorage);
    const next = storage.markStepViewed(boardTour, boardTour.steps[0]);

    expect(next.tours[boardTour.id].steps['board.step.one'].status).toBe('viewed');
    expect(storage.read().tours[boardTour.id].steps['board.step.one'].status).toBe('viewed');
  });

  it('preserves completed status and reactivates only the tour whose version changed', () => {
    const storage = createGuidedHelpStorage();
    storage.completeStep(boardTour, boardTour.steps[0]);
    storage.completeStep(boardTour, boardTour.steps[1]);
    storage.completeStep(metricsTour, metricsTour.steps[0]);

    expect(resolveFirstEligibleStep(storage.read(), [boardTour, metricsTour], { surface: 'board' })).toBeNull();
    expect(resolveFirstEligibleStep(storage.read(), [boardTour, metricsTour], { surface: 'metrics' })).toBeNull();

    const bumpedBoardTour: GuidedHelpTour = { ...boardTour, version: '2' };
    const boardStep = resolveFirstEligibleStep(storage.read(), [bumpedBoardTour, metricsTour], { surface: 'board' });
    const metricsStep = resolveFirstEligibleStep(storage.read(), [bumpedBoardTour, metricsTour], {
      surface: 'metrics',
    });

    expect(boardStep?.tour.id).toBe(boardTour.id);
    expect(boardStep?.step.id).toBe('board.step.one');
    expect(metricsStep).toBeNull();
  });

  it('blocks tours with Skip all until undo or reset restores eligibility', () => {
    const storage = createGuidedHelpStorage();

    expect(resolveFirstEligibleStep(storage.read(), [boardTour], { surface: 'board' })?.step.id).toBe(
      'board.step.one',
    );

    storage.skipAll();
    expect(storage.read().skippedAll).toBe(true);
    expect(resolveFirstEligibleStep(storage.read(), [boardTour], { surface: 'board' })).toBeNull();

    storage.undoSkipAll();
    expect(storage.read().skippedAll).toBe(false);
    expect(resolveFirstEligibleStep(storage.read(), [boardTour], { surface: 'board' })?.step.id).toBe(
      'board.step.one',
    );

    storage.skipAll();
    storage.resetTour(boardTour.id);
    expect(storage.read().skippedAll).toBe(false);
    expect(resolveFirstEligibleStep(storage.read(), [boardTour], { surface: 'board' })?.step.id).toBe(
      'board.step.one',
    );
  });

  it('resets all tours and clears Skip all in one local-first update', () => {
    const storage = createGuidedHelpStorage();
    storage.completeStep(boardTour, boardTour.steps[0]);
    storage.completeStep(metricsTour, metricsTour.steps[0]);
    storage.skipAll();

    const next = storage.resetAllTours();

    expect(next.skippedAll).toBe(false);
    expect(next.tours).toEqual({});
    expect(resolveFirstEligibleStep(storage.read(), [boardTour], { surface: 'board' })?.step.id).toBe(
      'board.step.one',
    );
  });
});

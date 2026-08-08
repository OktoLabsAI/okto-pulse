import { describe, expect, it } from 'vitest';

import {
  buildIdeationItems,
  buildRefinementItems,
} from '../ContextSelector';

const choiceOnlyAnswers = [
  {
    question: 'Which delivery mode?',
    answer: null,
    asked_by: 'agent-1',
    answered_at: '2026-07-27T12:00:00Z',
    choices: [{ id: 'safe', label: 'Safe rollout' }],
    selected: ['safe'],
  },
  {
    question: 'Which fallback?',
    answer: '',
    asked_by: 'agent-1',
    answered_at: '2026-07-27T12:01:00Z',
    choices: [{ id: 'manual', label: 'Manual fallback' }],
    selected: ['manual'],
  },
  {
    question: 'Payload without receipt?',
    answer: 'This must not be propagated.',
    asked_by: 'agent-1',
    answered_at: null,
    choices: [{ id: 'stale', label: 'Stale selection' }],
    selected: ['stale'],
  },
];

describe('ContextSelector Q&A decisions', () => {
  it('distinguishes legacy scope ambiguity from governed quality ambiguity', () => {
    const scopeItem = buildIdeationItems({
      scope_assessment: { domains: 2, ambiguity: 3, dependencies: 1 },
      complexity: 'medium',
    }).find((item) => item.id === 'scope_assessment');

    expect(scopeItem?.label).toContain('SA:3');
    expect(scopeItem?.content).toContain('Scope Ambiguity: 3/5');
  });

  it.each([
    ['ideation', () => buildIdeationItems({ qa_items: choiceOnlyAnswers })],
    ['refinement', () => buildRefinementItems({ qa_items: choiceOnlyAnswers })],
  ])(
    'includes %s choice-only answers only when answered_at is present',
    (_entityType, buildItems) => {
      const decisions = buildItems().filter((item) => item.category === 'Q&A Decisions');

      expect(decisions).toHaveLength(2);
      expect(decisions[0].content).toContain('**Selected:** Safe rollout');
      expect(decisions[0].content).not.toContain('**A:**');
      expect(decisions[1].content).toContain('**Selected:** Manual fallback');
      expect(decisions[1].content).not.toContain('**A:**');
      expect(decisions.map((item) => item.content).join('\n')).not.toContain(
        'This must not be propagated.',
      );
      expect(decisions.map((item) => item.content).join('\n')).not.toContain(
        'Stale selection',
      );
    },
  );
});

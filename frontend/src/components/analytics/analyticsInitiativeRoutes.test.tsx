import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { BoardKgCognitiveStatus } from './analyticsCanonicalTypes';
import type { DeliveryIntelligenceFilters } from './analyticsDeliveryTypes';
import type { CanonicalCoverageQueryState } from './canonicalCoverageQueryState';
import type { FlowHealthRouteFilters } from './flowHealthQueryState';

const apiMock = vi.hoisted(() => ({
  getBoard: vi.fn(),
  exportOverviewCsv: vi.fn(),
  exportBoardCsv: vi.fn(),
  exportEntityCsv: vi.fn(),
  exportBoardFlowHealthCsv: vi.fn(),
}));

vi.mock('@/services/api', () => ({ useDashboardApi: () => apiMock }));
vi.mock('./OverviewDashboard', () => ({ OverviewDashboard: () => <div data-testid="overview-route" /> }));
vi.mock('./EntityDetail', () => ({ EntityDetail: () => <div data-testid="entity-route" /> }));
vi.mock('./BoardDashboard', () => ({
  BoardDashboard: ({
    from,
    to,
    onOpenCanonicalCoverage,
    onOpenFlowHealth,
    onOpenDeliveryIntelligence,
    onOpenKgEffectiveness,
  }: {
    from: string;
    to: string;
    onOpenCanonicalCoverage: (query: CanonicalCoverageQueryState) => void;
    onOpenFlowHealth: () => void;
    onOpenDeliveryIntelligence: () => void;
    onOpenKgEffectiveness: () => void;
  }) => (
    <section data-testid="board-dashboard-route">
      <button
        type="button"
        onClick={() => onOpenCanonicalCoverage({
          from,
          to,
          lifecycle: 'current',
          outcome: 'uncovered',
          search: 'missing evidence',
        })}
      >
        Open A3 full view
      </button>
      <button type="button" onClick={onOpenFlowHealth}>Open A4 full view</button>
      <button type="button" onClick={onOpenDeliveryIntelligence}>Open A5 full view</button>
      <button type="button" onClick={onOpenKgEffectiveness}>Open A6 full view</button>
    </section>
  ),
}));
vi.mock('./CanonicalCoverageRoute', () => ({
  CanonicalCoverageRoute: ({
    queryState,
  }: {
    queryState: CanonicalCoverageQueryState;
  }) => (
    <div data-testid="canonical-coverage-route">
      coverage:{queryState.from}|{queryState.to}|{queryState.lifecycle}|{queryState.outcome}|{queryState.search}
    </div>
  ),
}));
vi.mock('./FlowHealthFullView', () => ({
  FlowHealthFullView: ({
    from,
    to,
    filters,
  }: {
    from: string;
    to: string;
    filters: FlowHealthRouteFilters;
  }) => (
    <div data-testid="flow-health-route">
      flow:{from}|{to}|{filters.search}|{filters.workType}|{filters.owner}|{filters.health}|{String(filters.blockersOnly)}
    </div>
  ),
}));
vi.mock('./FlowHealthSettingsPage', () => ({
  FlowHealthSettingsPage: () => <div data-testid="flow-health-settings-route" />,
}));
vi.mock('./DeliveryIntelligenceFullView', () => ({
  DeliveryIntelligenceFullView: ({
    from,
    to,
    initialFilters,
  }: {
    from: string;
    to: string;
    initialFilters: DeliveryIntelligenceFilters;
  }) => (
    <div data-testid="delivery-intelligence-route">
      delivery:{from}|{to}|{initialFilters.sprintId}|{initialFilters.lane}|{initialFilters.role}|{initialFilters.contributionView}
    </div>
  ),
}));
vi.mock('./KgEffectivenessFullView', () => ({
  KgEffectivenessFullView: ({
    from,
    to,
    initialCognitiveStatus,
    initialArtifactTypes,
    pageLimit,
  }: {
    from: string;
    to: string;
    initialCognitiveStatus: readonly BoardKgCognitiveStatus[];
    initialArtifactTypes: readonly string[];
    pageLimit: number;
  }) => (
    <div data-testid="kg-effectiveness-route">
      kg:{from}|{to}|{initialCognitiveStatus.join(',')}|{initialArtifactTypes.join(',')}|{pageLimit}
    </div>
  ),
}));

import { AnalyticsPage } from './AnalyticsPage';

const boardPath = '/analytics/boards/board-1';
const sharedQuery = [
  'from=2026-07-01',
  'to=2026-07-31',
  'search=checkout',
  'work_type=card',
  'owner=Maya',
  'health=blocked',
  'blockers_only=true',
  'sprint_id=sprint-7',
  'lane=hotfix',
  'role=reviewer',
  'contribution_view=aggregates',
  'cognitive_status=pending',
  'cognitive_status=failed',
  'artifact_type=task',
  'artifact_type=spec',
  'limit=37',
].join('&');

function moveWithPopstate(url: string): void {
  act(() => {
    window.history.pushState({}, '', url);
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
}

describe('Analytics A3-A6 route integration', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getBoard.mockResolvedValue({ name: 'E2E' });
    window.history.replaceState({}, '', `${boardPath}?${sharedQuery}`);
  });

  it.each([
    {
      initiative: 'A3',
      url: `${boardPath}/canonical-coverage?from=2026-06-01&to=2026-06-30&lifecycle=current&outcome=uncovered&search=payments`,
      testId: 'canonical-coverage-route',
      content: 'coverage:2026-06-01|2026-06-30|current|uncovered|payments',
    },
    {
      initiative: 'A4',
      url: `${boardPath}/flow-health?from=2026-06-01&to=2026-06-30&search=blocked&work_type=spec&owner=Noah&health=at_risk&blockers_only=true`,
      testId: 'flow-health-route',
      content: 'flow:2026-06-01|2026-06-30|blocked|spec|Noah|at_risk|true',
    },
    {
      initiative: 'A5',
      url: `${boardPath}/delivery-intelligence?from=2026-06-01&to=2026-06-30&sprint_id=sprint-9&lane=normal&role=developer&contribution_view=self`,
      testId: 'delivery-intelligence-route',
      content: 'delivery:2026-06-01|2026-06-30|sprint-9|normal|developer|self',
    },
    {
      initiative: 'A6',
      url: `${boardPath}/kg-effectiveness?from=2026-06-01&to=2026-06-30&cognitive_status=failed&cognitive_status=pending&artifact_type=spec&artifact_type=task&limit=37`,
      testId: 'kg-effectiveness-route',
      content: 'kg:2026-06-01|2026-06-30|failed,pending|spec,task|37',
    },
  ])('restores a copied $initiative deep-link with its query state', async ({ url, testId, content }) => {
    window.history.replaceState({}, '', url);
    render(<AnalyticsPage />);

    expect(await screen.findByTestId(testId)).toHaveTextContent(content);
  });

  it('wires every board Open full view callback to its canonical route without dropping scoped query state', async () => {
    render(<AnalyticsPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Open A3 full view' }));
    expect(await screen.findByTestId('canonical-coverage-route')).toHaveTextContent(
      'coverage:2026-07-01|2026-07-31|current|uncovered|missing evidence',
    );
    expect(window.location.pathname).toBe(`${boardPath}/canonical-coverage`);
    expect(new URLSearchParams(window.location.search).get('search')).toBe('missing evidence');
    expect(new URLSearchParams(window.location.search).get('outcome')).toBe('uncovered');

    moveWithPopstate(`${boardPath}?${sharedQuery}`);
    fireEvent.click(await screen.findByRole('button', { name: 'Open A4 full view' }));
    expect(await screen.findByTestId('flow-health-route')).toHaveTextContent(
      'flow:2026-07-01|2026-07-31|checkout|card|Maya|blocked|true',
    );
    expect(window.location.pathname).toBe(`${boardPath}/flow-health`);
    expect(new URLSearchParams(window.location.search).get('owner')).toBe('Maya');
    expect(new URLSearchParams(window.location.search).get('blockers_only')).toBe('true');

    moveWithPopstate(`${boardPath}?${sharedQuery}`);
    fireEvent.click(await screen.findByRole('button', { name: 'Open A5 full view' }));
    expect(await screen.findByTestId('delivery-intelligence-route')).toHaveTextContent(
      'delivery:2026-07-01|2026-07-31|sprint-7|hotfix|reviewer|aggregates',
    );
    expect(window.location.pathname).toBe(`${boardPath}/delivery-intelligence`);
    expect(new URLSearchParams(window.location.search).get('sprint_id')).toBe('sprint-7');
    expect(new URLSearchParams(window.location.search).get('contribution_view')).toBe('aggregates');

    moveWithPopstate(`${boardPath}?${sharedQuery}`);
    fireEvent.click(await screen.findByRole('button', { name: 'Open A6 full view' }));
    expect(await screen.findByTestId('kg-effectiveness-route')).toHaveTextContent(
      'kg:2026-07-01|2026-07-31|pending,failed|task,spec|37',
    );
    expect(window.location.pathname).toBe(`${boardPath}/kg-effectiveness`);
    const kgQuery = new URLSearchParams(window.location.search);
    expect(kgQuery.getAll('cognitive_status')).toEqual(['failed', 'pending']);
    expect(kgQuery.getAll('artifact_type')).toEqual(['spec', 'task']);
    expect(kgQuery.get('limit')).toBe('37');
  });

  it('rehydrates each initiative state when browser history emits popstate', async () => {
    render(<AnalyticsPage />);

    moveWithPopstate(`${boardPath}/canonical-coverage?from=2026-05-01&to=2026-05-31&lifecycle=active&outcome=skipped&search=waiver`);
    expect(await screen.findByTestId('canonical-coverage-route')).toHaveTextContent(
      'coverage:2026-05-01|2026-05-31|active|skipped|waiver',
    );

    moveWithPopstate(`${boardPath}/flow-health?from=2026-04-01&to=2026-04-30&work_type=card&owner=Ada&health=blocked`);
    expect(await screen.findByTestId('flow-health-route')).toHaveTextContent(
      'flow:2026-04-01|2026-04-30||card|Ada|blocked|false',
    );

    moveWithPopstate(`${boardPath}/delivery-intelligence?from=2026-03-01&to=2026-03-31&sprint_id=sprint-3&lane=normal&role=qa`);
    expect(await screen.findByTestId('delivery-intelligence-route')).toHaveTextContent(
      'delivery:2026-03-01|2026-03-31|sprint-3|normal|qa|self_and_aggregates',
    );

    moveWithPopstate(`${boardPath}/kg-effectiveness?from=2026-02-01&to=2026-02-28&cognitive_status=consolidated&artifact_type=decision&limit=12`);
    await waitFor(() => expect(screen.getByTestId('kg-effectiveness-route')).toHaveTextContent(
      'kg:2026-02-01|2026-02-28|consolidated|decision|12',
    ));
  });
});

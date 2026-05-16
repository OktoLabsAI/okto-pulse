import type { GuidedHelpRegistry, GuidedHelpSurface, GuidedHelpTour } from './types';

const tours: GuidedHelpTour[] = [
  {
    id: 'board.overview',
    title: 'Board overview',
    surface: 'board',
    version: '1',
    order: 10,
    steps: [
      {
        id: 'board.navigation.tabs',
        title: 'Move across work areas',
        body: 'Use the main tabs to switch between stories, discovery, specs, sprints, and task execution.',
        anchor: 'board.tabs',
        kind: 'navigation',
        placement: 'bottom',
        order: 10,
      },
      {
        id: 'board.refresh',
        title: 'Refresh shared work',
        body: 'Refresh the board after agents or teammates make progress so the current workspace stays in sync.',
        anchor: 'board.refresh',
        kind: 'feature',
        placement: 'bottom',
        order: 20,
      },
    ],
  },
  {
    id: 'specs.resources',
    title: 'Spec resources',
    surface: 'specs',
    version: '1',
    order: 20,
    steps: [
      {
        id: 'specs.resources.tabs',
        title: 'Use resources as first-class inputs',
        body: 'Rules, contracts, architecture, mockups, and knowledge entries keep implementation traceable.',
        anchor: 'specs.resources.tabs',
        kind: 'validation',
        placement: 'bottom',
        order: 10,
      },
    ],
  },
  {
    id: 'tasks.workflow',
    title: 'Task workflow',
    surface: 'tasks',
    version: '1',
    order: 30,
    steps: [
      {
        id: 'tasks.validation.flow',
        title: 'Move tasks through validation',
        body: 'Normal tasks move to validation with execution evidence before another reviewer approves them.',
        anchor: 'tasks.validation.column',
        kind: 'validation',
        placement: 'top',
        order: 10,
      },
    ],
  },
  {
    id: 'kg.discovery',
    title: 'Knowledge Graph',
    surface: 'kg',
    version: '1',
    order: 40,
    steps: [
      {
        id: 'kg.discovery.search',
        title: 'Explore related context',
        body: 'Search and graph views help connect decisions, requirements, bugs, and learning across the board.',
        anchor: 'kg.discovery.search',
        kind: 'feature',
        placement: 'right',
        order: 10,
      },
    ],
  },
  {
    id: 'metrics.overview',
    title: 'Metrics overview',
    surface: 'metrics',
    version: '1',
    order: 50,
    steps: [
      {
        id: 'metrics.adoption.summary',
        title: 'Track adoption signals',
        body: 'Metrics summarize product usage and health signals using privacy-aware local-first settings.',
        anchor: 'metrics.overview.summary',
        kind: 'feature',
        placement: 'left',
        order: 10,
      },
    ],
  },
  {
    id: 'agents.coordination',
    title: 'Agents',
    surface: 'agents',
    version: '1',
    order: 60,
    steps: [
      {
        id: 'agents.assignment',
        title: 'Coordinate agent work',
        body: 'Agent views help clarify ownership and keep execution aligned with the board flow.',
        anchor: 'agents.modal.entry',
        kind: 'feature',
        placement: 'left',
        order: 10,
      },
    ],
  },
  {
    id: 'help.guided_tours',
    title: 'Guided tours',
    surface: 'help',
    version: '1',
    order: 70,
    steps: [
      {
        id: 'help.guided_tours.controls',
        title: 'Replay or reset tours',
        body: 'The Help area lets users restart tours or undo Skip all without touching board content.',
        anchor: 'help.guided_tours',
        kind: 'replay',
        placement: 'right',
        order: 10,
      },
    ],
  },
];

export const guidedHelpRegistry: GuidedHelpRegistry = { tours };

export function getToursForSurface(
  registry: GuidedHelpRegistry,
  surface: GuidedHelpSurface,
): GuidedHelpTour[] {
  return registry.tours
    .filter((tour) => tour.surface === surface)
    .sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
}

export function getTourById(registry: GuidedHelpRegistry, tourId: string): GuidedHelpTour | null {
  return registry.tours.find((tour) => tour.id === tourId) ?? null;
}

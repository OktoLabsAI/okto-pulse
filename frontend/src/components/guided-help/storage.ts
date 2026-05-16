import type {
  GuidedHelpProgressState,
  GuidedHelpResolvedStep,
  GuidedHelpStep,
  GuidedHelpStoredStepProgress,
  GuidedHelpStoredTourProgress,
  GuidedHelpTour,
  GuidedHelpTourProgressStatus,
  GuidedHelpTourSummary,
} from './types';

export const GUIDED_HELP_STORAGE_KEY = 'okto.guided-help.progress.v1';
export const GUIDED_HELP_PROGRESS_EVENT = 'okto:guided-help-progress-changed';

type StorageLike = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

function nowIso(): string {
  return new Date().toISOString();
}

export function createEmptyProgressState(): GuidedHelpProgressState {
  return {
    schemaVersion: 1,
    updatedAt: nowIso(),
    skippedAll: false,
    tours: {},
  };
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function normalizeStepProgress(value: unknown): GuidedHelpStoredStepProgress | null {
  if (!isObject(value)) return null;
  const status = value.status;
  const version = value.version;
  if (status !== 'viewed' && status !== 'completed' && status !== 'skipped') return null;
  if (typeof version !== 'string' || version.length === 0) return null;
  return {
    version,
    status,
    lastSeenAt: typeof value.lastSeenAt === 'string' ? value.lastSeenAt : undefined,
    completedAt: typeof value.completedAt === 'string' ? value.completedAt : undefined,
    skippedAt: typeof value.skippedAt === 'string' ? value.skippedAt : undefined,
  };
}

function normalizeTourProgress(value: unknown): GuidedHelpStoredTourProgress | null {
  if (!isObject(value)) return null;
  const version = value.version;
  const status = value.status;
  if (typeof version !== 'string' || version.length === 0) return null;
  if (status !== 'in_progress' && status !== 'completed' && status !== 'skipped') return null;

  const steps: Record<string, GuidedHelpStoredStepProgress> = {};
  if (isObject(value.steps)) {
    for (const [stepId, rawStep] of Object.entries(value.steps)) {
      const normalized = normalizeStepProgress(rawStep);
      if (normalized) steps[stepId] = normalized;
    }
  }

  return {
    version,
    status,
    steps,
    lastSeenAt: typeof value.lastSeenAt === 'string' ? value.lastSeenAt : undefined,
    completedAt: typeof value.completedAt === 'string' ? value.completedAt : undefined,
    skippedAt: typeof value.skippedAt === 'string' ? value.skippedAt : undefined,
  };
}

export function normalizeProgressState(value: unknown): GuidedHelpProgressState {
  if (!isObject(value)) return createEmptyProgressState();

  const tours: GuidedHelpProgressState['tours'] = {};
  if (isObject(value.tours)) {
    for (const [tourId, rawTour] of Object.entries(value.tours)) {
      const normalized = normalizeTourProgress(rawTour);
      if (normalized) tours[tourId] = normalized;
    }
  }

  return {
    schemaVersion: 1,
    updatedAt: typeof value.updatedAt === 'string' ? value.updatedAt : nowIso(),
    skippedAll: value.skippedAll === true,
    skippedAllAt: typeof value.skippedAllAt === 'string' ? value.skippedAllAt : undefined,
    tours,
  };
}

function defaultStorage(): StorageLike | null {
  if (typeof window === 'undefined') return null;
  try {
    const probeKey = `${GUIDED_HELP_STORAGE_KEY}:probe`;
    window.localStorage.setItem(probeKey, '1');
    window.localStorage.removeItem(probeKey);
    return window.localStorage;
  } catch {
    return null;
  }
}

function cloneState(state: GuidedHelpProgressState): GuidedHelpProgressState {
  const tours: GuidedHelpProgressState['tours'] = {};
  for (const [tourId, tour] of Object.entries(state.tours)) {
    tours[tourId] = {
      ...tour,
      steps: { ...tour.steps },
    };
  }
  return {
    ...state,
    tours,
  };
}

function dispatchProgressEvent(): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent(GUIDED_HELP_PROGRESS_EVENT));
}

function deriveTourStatus(
  tour: GuidedHelpTour,
  steps: Record<string, GuidedHelpStoredStepProgress>,
): Exclude<GuidedHelpTourProgressStatus, 'not_started'> {
  const currentSteps = tour.steps
    .slice()
    .sort((a, b) => (a.order ?? 0) - (b.order ?? 0))
    .map((step) => steps[step.id])
    .filter((step): step is GuidedHelpStoredStepProgress => Boolean(step) && step.version === tour.version);

  if (currentSteps.length === 0) return 'in_progress';
  if (currentSteps.length === tour.steps.length && currentSteps.every((step) => step.status === 'completed')) {
    return 'completed';
  }
  if (
    currentSteps.length === tour.steps.length &&
    currentSteps.every((step) => step.status === 'completed' || step.status === 'skipped')
  ) {
    return 'skipped';
  }
  return 'in_progress';
}

function ensureTourProgress(
  state: GuidedHelpProgressState,
  tour: GuidedHelpTour,
): GuidedHelpStoredTourProgress {
  const existing = state.tours[tour.id];
  const tourProgress =
    existing?.version === tour.version
      ? { ...existing, steps: { ...existing.steps } }
      : { version: tour.version, status: 'in_progress' as const, steps: {} };
  state.tours[tour.id] = tourProgress;
  return tourProgress;
}

function resolveBackendState(backend: StorageLike | null, memoryState: GuidedHelpProgressState): GuidedHelpProgressState {
  if (!backend) return memoryState;
  try {
    const raw = backend.getItem(GUIDED_HELP_STORAGE_KEY);
    if (!raw) return memoryState;
    return normalizeProgressState(JSON.parse(raw));
  } catch {
    return memoryState;
  }
}

function getCurrentStepProgress(
  state: GuidedHelpProgressState,
  tour: GuidedHelpTour,
  step: GuidedHelpStep,
): GuidedHelpStoredStepProgress | null {
  const tourProgress = state.tours[tour.id];
  const stepProgress = tourProgress?.steps[step.id];
  if (!tourProgress || tourProgress.version !== tour.version) return null;
  if (!stepProgress || stepProgress.version !== tour.version) return null;
  return stepProgress;
}

export function createGuidedHelpStorage(storage: StorageLike | null = defaultStorage()) {
  let memoryState = createEmptyProgressState();

  const read = (): GuidedHelpProgressState => resolveBackendState(storage, memoryState);

  const persist = (next: GuidedHelpProgressState): GuidedHelpProgressState => {
    memoryState = next;
    try {
      storage?.setItem(GUIDED_HELP_STORAGE_KEY, JSON.stringify(next));
    } catch {
      storage = null;
    }
    dispatchProgressEvent();
    return next;
  };

  const update = (mutate: (next: GuidedHelpProgressState, timestamp: string) => void): GuidedHelpProgressState => {
    const next = cloneState(read());
    const timestamp = nowIso();
    mutate(next, timestamp);
    next.updatedAt = timestamp;
    return persist(next);
  };

  return {
    read,
    markStepViewed(tour: GuidedHelpTour, step: GuidedHelpStep) {
      return update((next, timestamp) => {
        const existing = getCurrentStepProgress(next, tour, step);
        if (existing) return;
        const tourProgress = ensureTourProgress(next, tour);
        tourProgress.steps[step.id] = {
          version: tour.version,
          status: 'viewed',
          lastSeenAt: timestamp,
        };
        tourProgress.status = deriveTourStatus(tour, tourProgress.steps);
        tourProgress.lastSeenAt = timestamp;
      });
    },
    completeStep(tour: GuidedHelpTour, step: GuidedHelpStep) {
      return update((next, timestamp) => {
        const tourProgress = ensureTourProgress(next, tour);
        tourProgress.steps[step.id] = {
          version: tour.version,
          status: 'completed',
          lastSeenAt: timestamp,
          completedAt: timestamp,
        };
        tourProgress.status = deriveTourStatus(tour, tourProgress.steps);
        tourProgress.lastSeenAt = timestamp;
        if (tourProgress.status === 'completed') tourProgress.completedAt = timestamp;
      });
    },
    skipStep(tour: GuidedHelpTour, step: GuidedHelpStep) {
      return update((next, timestamp) => {
        const tourProgress = ensureTourProgress(next, tour);
        tourProgress.steps[step.id] = {
          version: tour.version,
          status: 'skipped',
          lastSeenAt: timestamp,
          skippedAt: timestamp,
        };
        tourProgress.status = deriveTourStatus(tour, tourProgress.steps);
        tourProgress.lastSeenAt = timestamp;
        if (tourProgress.status === 'skipped') tourProgress.skippedAt = timestamp;
      });
    },
    skipAll() {
      return update((next, timestamp) => {
        next.skippedAll = true;
        next.skippedAllAt = timestamp;
      });
    },
    undoSkipAll() {
      return update((next) => {
        next.skippedAll = false;
        delete next.skippedAllAt;
      });
    },
    resetTour(tourId: string) {
      return update((next) => {
        delete next.tours[tourId];
        next.skippedAll = false;
        delete next.skippedAllAt;
      });
    },
    replayTour(tour: GuidedHelpTour) {
      return update((next) => {
        delete next.tours[tour.id];
        next.skippedAll = false;
        delete next.skippedAllAt;
      });
    },
    getSummaries(tours: GuidedHelpTour[]) {
      const state = read();
      return tours.map((tour) => getGuidedHelpTourSummary(state, tour));
    },
  };
}

export function getGuidedHelpTourSummary(
  state: GuidedHelpProgressState,
  tour: GuidedHelpTour,
): GuidedHelpTourSummary {
  const saved = state.tours[tour.id];
  const versionChanged = Boolean(saved && saved.version !== tour.version);
  const stepProgress = versionChanged ? {} : (saved?.steps ?? {});
  const currentSteps = tour.steps.map((step) => stepProgress[step.id]).filter(Boolean);
  const completedSteps = currentSteps.filter((step) => step.status === 'completed').length;
  const skippedSteps = currentSteps.filter((step) => step.status === 'skipped').length;
  const viewedSteps = currentSteps.filter((step) => step.status === 'viewed').length;

  let status: GuidedHelpTourProgressStatus = 'not_started';
  if (versionChanged || !saved || currentSteps.length === 0) {
    status = 'not_started';
  } else if (completedSteps === tour.steps.length) {
    status = 'completed';
  } else if (completedSteps + skippedSteps === tour.steps.length) {
    status = 'skipped';
  } else {
    status = 'in_progress';
  }

  return {
    tourId: tour.id,
    title: tour.title,
    surface: tour.surface,
    version: tour.version,
    status,
    blockedBySkipAll: state.skippedAll,
    completedSteps,
    skippedSteps,
    viewedSteps,
    totalSteps: tour.steps.length,
    versionChanged,
  };
}

export function resolveFirstEligibleStep(
  state: GuidedHelpProgressState,
  tours: GuidedHelpTour[],
  options: { surface?: string; tourId?: string } = {},
): GuidedHelpResolvedStep | null {
  if (state.skippedAll) return null;

  const orderedTours = tours
    .filter((tour) => !options.surface || tour.surface === options.surface)
    .filter((tour) => !options.tourId || tour.id === options.tourId)
    .sort((a, b) => (a.order ?? 0) - (b.order ?? 0));

  for (const tour of orderedTours) {
    const tourProgress = state.tours[tour.id];
    const versionChanged = Boolean(tourProgress && tourProgress.version !== tour.version);
    const orderedSteps = tour.steps.slice().sort((a, b) => (a.order ?? 0) - (b.order ?? 0));

    for (let index = 0; index < orderedSteps.length; index += 1) {
      const step = orderedSteps[index];
      const stepProgress = versionChanged ? null : tourProgress?.steps[step.id];
      if (!stepProgress || stepProgress.version !== tour.version || stepProgress.status === 'viewed') {
        return {
          tour,
          step,
          stepIndex: index,
          totalSteps: orderedSteps.length,
        };
      }
    }
  }

  return null;
}

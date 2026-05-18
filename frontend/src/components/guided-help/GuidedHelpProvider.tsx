import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';

import { GuidedHelpPopover } from './GuidedHelpPopover';
import { isUsableAnchorRect } from './positioning';
import { getTourById } from './registry';
import {
  createGuidedHelpStorage,
  getGuidedHelpTourSummary,
  GUIDED_HELP_PROGRESS_EVENT,
  resolveFirstEligibleStep,
} from './storage';
import type {
  GuidedHelpContextValue,
  GuidedHelpProviderProps,
  GuidedHelpResolvedStep,
  GuidedHelpStep,
  GuidedHelpTour,
} from './types';

const GuidedHelpContext = createContext<GuidedHelpContextValue | null>(null);

interface ManualStepTarget {
  tourId: string;
  stepId: string;
}

function hasSuppression(props: GuidedHelpProviderProps['suppressWhen']): boolean {
  return Boolean(props && Object.values(props).some(Boolean));
}

function resolveStepById(tour: GuidedHelpTour, stepId?: string): GuidedHelpStep | null {
  if (!stepId) return null;
  return tour.steps.find((step) => step.id === stepId) ?? null;
}

function orderedSteps(tour: GuidedHelpTour): GuidedHelpStep[] {
  return tour.steps.slice().sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
}

function resolveManualStep(registry: GuidedHelpProviderProps['registry'], target: ManualStepTarget | null) {
  if (!target) return null;
  const tour = getTourById(registry, target.tourId);
  if (!tour) return null;
  const steps = orderedSteps(tour);
  const stepIndex = steps.findIndex((step) => step.id === target.stepId);
  if (stepIndex < 0) return null;
  return {
    tour,
    step: steps[stepIndex],
    stepIndex,
    totalSteps: steps.length,
  };
}

function findAnchorElement(anchor: string): HTMLElement | null {
  if (typeof document === 'undefined') return null;
  const candidates = Array.from(document.querySelectorAll<HTMLElement>('[data-tour-id]'));
  return candidates.find((element) => element.dataset.tourId === anchor) ?? null;
}

function readAnchorRect(anchor: string): DOMRect | null {
  const element = findAnchorElement(anchor);
  if (!element) return null;

  const style = window.getComputedStyle(element);
  if (style.display === 'none' || style.visibility === 'hidden') return null;

  let rect = element.getBoundingClientRect();
  if (isUsableAnchorRect(rect)) return rect;

  if (typeof element.scrollIntoView === 'function') {
    try {
      element.scrollIntoView({ block: 'center', inline: 'center', behavior: 'smooth' });
    } catch {
      element.scrollIntoView();
    }
  }

  rect = element.getBoundingClientRect();
  return isUsableAnchorRect(rect) ? rect : null;
}

export function GuidedHelpProvider({
  children,
  registry,
  surface,
  suppressWhen,
  storage,
  telemetryAdapter,
  enabled = true,
}: GuidedHelpProviderProps) {
  const storageApi = useMemo(() => storage ?? createGuidedHelpStorage(), [storage]);
  const [progress, setProgress] = useState(() => storageApi.read());
  const [requestedTourId, setRequestedTourId] = useState<string | null>(null);
  const [manualStepTarget, setManualStepTarget] = useState<ManualStepTarget | null>(null);
  const [anchorRect, setAnchorRect] = useState<DOMRect | null>(null);
  const isSuppressed = !enabled || hasSuppression(suppressWhen);
  const tours = registry.tours;

  useEffect(() => {
    const refresh = () => setProgress(storageApi.read());
    window.addEventListener(GUIDED_HELP_PROGRESS_EVENT, refresh);
    return () => window.removeEventListener(GUIDED_HELP_PROGRESS_EVENT, refresh);
  }, [storageApi]);

  useEffect(() => {
    setProgress(storageApi.read());
  }, [storageApi]);

  const activeResolved = useMemo(() => {
    if (isSuppressed) return null;
    const manual = resolveManualStep(registry, manualStepTarget);
    if (manual) return manual;
    const requested = requestedTourId
      ? resolveFirstEligibleStep(progress, tours, { tourId: requestedTourId })
      : null;
    return requested ?? resolveFirstEligibleStep(progress, tours, { surface });
  }, [isSuppressed, manualStepTarget, progress, registry, requestedTourId, surface, tours]);

  useEffect(() => {
    if (!requestedTourId) return;
    if (!activeResolved || activeResolved.tour.id !== requestedTourId) {
      setRequestedTourId(null);
    }
  }, [activeResolved, requestedTourId]);

  useEffect(() => {
    if (!activeResolved) {
      setAnchorRect(null);
      return undefined;
    }

    const updateAnchorRect = () => {
      const nextRect = readAnchorRect(activeResolved.step.anchor);
      setAnchorRect(nextRect);
      return nextRect;
    };

    let attempts = 0;
    const maxAttempts = 24;
    let retryTimer: number | undefined;
    const retryUntilAnchored = () => {
      attempts += 1;
      const rect = updateAnchorRect();
      if (rect || attempts >= maxAttempts) {
        if (retryTimer !== undefined) window.clearInterval(retryTimer);
        retryTimer = undefined;
      }
    };

    const observer =
      typeof MutationObserver !== 'undefined'
        ? new MutationObserver(() => {
            attempts = 0;
            retryUntilAnchored();
          })
        : null;

    const firstRect = updateAnchorRect();
    if (!firstRect && retryTimer === undefined) {
      retryTimer = window.setInterval(retryUntilAnchored, 250);
    }
    observer?.observe(document.body, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['class', 'style', 'data-tour-id'],
    });
    window.addEventListener('resize', updateAnchorRect);
    window.addEventListener('scroll', updateAnchorRect, true);
    return () => {
      if (retryTimer !== undefined) window.clearInterval(retryTimer);
      observer?.disconnect();
      window.removeEventListener('resize', updateAnchorRect);
      window.removeEventListener('scroll', updateAnchorRect, true);
    };
  }, [activeResolved]);

  useEffect(() => {
    if (!activeResolved) return;
    const savedTour = progress.tours[activeResolved.tour.id];
    const savedStep = savedTour?.steps[activeResolved.step.id];
    const alreadyTracked =
      savedTour?.version === activeResolved.tour.version &&
      savedStep?.version === activeResolved.tour.version &&
      Boolean(savedStep.status);
    if (alreadyTracked) return;
    setProgress(storageApi.markStepViewed(activeResolved.tour, activeResolved.step));
    void telemetryAdapter?.emit({
      action: 'viewed',
      tour_surface: activeResolved.tour.surface,
      step_kind: activeResolved.step.kind,
      status: 'success',
    });
  }, [activeResolved, progress.tours, storageApi, telemetryAdapter]);

  const refresh = useCallback(() => {
    setProgress(storageApi.read());
  }, [storageApi]);

  const resolveActionTarget = useCallback(
    (tourId?: string, stepId?: string): GuidedHelpResolvedStep | null => {
      if (!tourId && !stepId) return activeResolved;
      const tour = tourId ? getTourById(registry, tourId) : activeResolved?.tour;
      if (!tour) return null;
      const step = resolveStepById(tour, stepId) ?? activeResolved?.step ?? tour.steps[0] ?? null;
      if (!step) return null;
      return {
        tour,
        step,
        stepIndex: tour.steps.findIndex((item) => item.id === step.id),
        totalSteps: tour.steps.length,
      };
    },
    [activeResolved, registry],
  );

  const startTour = useCallback((tourId: string) => {
    setManualStepTarget(null);
    setRequestedTourId(tourId);
  }, []);

  const completeStep = useCallback(
    (tourId?: string, stepId?: string) => {
      const target = resolveActionTarget(tourId, stepId);
      if (!target) return;
      const next = storageApi.completeStep(target.tour, target.step);
      setManualStepTarget(null);
      setProgress(next);
      const completed = resolveFirstEligibleStep(next, [target.tour], { tourId: target.tour.id }) === null;
      void telemetryAdapter?.emit({
        action: completed ? 'completed' : 'step_completed',
        tour_surface: target.tour.surface,
        step_kind: target.step.kind,
        status: 'success',
      });
    },
    [resolveActionTarget, storageApi, telemetryAdapter],
  );

  const skipStep = useCallback(
    (tourId?: string, stepId?: string) => {
      const target = resolveActionTarget(tourId, stepId);
      if (!target) return;
      setManualStepTarget(null);
      setProgress(storageApi.skipStep(target.tour, target.step));
      void telemetryAdapter?.emit({
        action: 'skipped_step',
        tour_surface: target.tour.surface,
        step_kind: target.step.kind,
        status: 'skipped',
      });
    },
    [resolveActionTarget, storageApi, telemetryAdapter],
  );

  const skipAll = useCallback(() => {
    const target = activeResolved;
    setRequestedTourId(null);
    setManualStepTarget(null);
    setProgress(storageApi.skipAll());
    void telemetryAdapter?.emit({
      action: 'skipped_all',
      tour_surface: target?.tour.surface ?? surface,
      step_kind: target?.step.kind ?? 'navigation',
      status: 'skipped',
    });
  }, [activeResolved, storageApi, surface, telemetryAdapter]);

  const undoSkipAll = useCallback(() => {
    setProgress(storageApi.undoSkipAll());
  }, [storageApi]);

  const resetTour = useCallback(
    (tourId: string) => {
      const tour = getTourById(registry, tourId);
      setManualStepTarget(null);
      setRequestedTourId(tourId);
      setProgress(storageApi.resetTour(tourId));
      if (tour) {
        void telemetryAdapter?.emit({
          action: 'reset',
          tour_surface: tour.surface,
          step_kind: tour.steps[0]?.kind ?? 'navigation',
          status: 'success',
        });
      }
    },
    [registry, storageApi, telemetryAdapter],
  );

  const resetAllTours = useCallback(() => {
    setManualStepTarget(null);
    setRequestedTourId(null);
    setProgress(storageApi.resetAllTours());
    void telemetryAdapter?.emit({
      action: 'reset',
      tour_surface: surface,
      step_kind: 'replay',
      status: 'success',
    });
  }, [storageApi, surface, telemetryAdapter]);

  const replayTour = useCallback(
    (tourId: string) => {
      const tour = getTourById(registry, tourId);
      if (!tour) return;
      setManualStepTarget(null);
      setRequestedTourId(tourId);
      setProgress(storageApi.replayTour(tour));
      void telemetryAdapter?.emit({
        action: 'replayed',
        tour_surface: tour.surface,
        step_kind: tour.steps[0]?.kind ?? 'replay',
        status: 'success',
      });
    },
    [registry, storageApi, telemetryAdapter],
  );

  const backStep = useCallback(() => {
    if (!activeResolved || activeResolved.stepIndex <= 0) return;
    const previousStep = orderedSteps(activeResolved.tour)[activeResolved.stepIndex - 1];
    if (!previousStep) return;
    setRequestedTourId(activeResolved.tour.id);
    setManualStepTarget({ tourId: activeResolved.tour.id, stepId: previousStep.id });
  }, [activeResolved]);

  const summaries = useMemo(
    () => tours.map((tour) => getGuidedHelpTourSummary(progress, tour)),
    [progress, tours],
  );

  const contextValue = useMemo<GuidedHelpContextValue>(
    () => ({
      activeTour: activeResolved?.tour ?? null,
      activeStep: activeResolved?.step ?? null,
      activeStepIndex: activeResolved?.stepIndex ?? -1,
      activeTotalSteps: activeResolved?.totalSteps ?? 0,
      surface,
      isSuppressed,
      skippedAll: progress.skippedAll,
      progress,
      summaries,
      startTour,
      completeStep,
      skipStep,
      skipAll,
      undoSkipAll,
      resetTour,
      resetAllTours,
      replayTour,
      refresh,
    }),
    [
      activeResolved,
      completeStep,
      isSuppressed,
      progress,
      refresh,
      replayTour,
      resetTour,
      resetAllTours,
      skipAll,
      skipStep,
      startTour,
      surface,
      summaries,
      undoSkipAll,
    ],
  );

  return (
    <GuidedHelpContext.Provider value={contextValue}>
      {children}
      {activeResolved && (
        <GuidedHelpPopover
          step={activeResolved.step}
          anchorRect={anchorRect}
          placement={activeResolved.step.placement}
          progress={{ current: activeResolved.stepIndex + 1, total: activeResolved.totalSteps }}
          canGoBack={activeResolved.stepIndex > 0}
          onBack={backStep}
          onNext={() => completeStep()}
          onDone={() => completeStep()}
          onSkipStep={() => skipStep()}
          onSkipAll={skipAll}
        />
      )}
    </GuidedHelpContext.Provider>
  );
}

export function useGuidedHelp(): GuidedHelpContextValue {
  const context = useContext(GuidedHelpContext);
  if (!context) {
    throw new Error('useGuidedHelp must be used inside <GuidedHelpProvider>.');
  }
  return context;
}

export function useOptionalGuidedHelp(): GuidedHelpContextValue | null {
  return useContext(GuidedHelpContext);
}

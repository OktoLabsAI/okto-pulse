import type { ReactNode } from 'react';

export type GuidedHelpSurface = 'board' | 'specs' | 'tasks' | 'kg' | 'metrics' | 'agents' | 'help';

export type GuidedHelpStepKind = 'navigation' | 'feature' | 'settings' | 'validation' | 'replay';

export type GuidedHelpPlacement = 'top' | 'right' | 'bottom' | 'left' | 'fallback';

export type GuidedHelpStepProgressStatus = 'viewed' | 'completed' | 'skipped';

export type GuidedHelpTourProgressStatus = 'not_started' | 'in_progress' | 'completed' | 'skipped';

export interface GuidedHelpStep {
  id: string;
  title: string;
  body: string;
  anchor: string;
  kind: GuidedHelpStepKind;
  placement?: GuidedHelpPlacement;
  order?: number;
}

export interface GuidedHelpTour {
  id: string;
  title: string;
  surface: GuidedHelpSurface;
  version: string;
  steps: GuidedHelpStep[];
  order?: number;
}

export interface GuidedHelpRegistry {
  tours: GuidedHelpTour[];
}

export interface GuidedHelpStoredStepProgress {
  version: string;
  status: GuidedHelpStepProgressStatus;
  lastSeenAt?: string;
  completedAt?: string;
  skippedAt?: string;
}

export interface GuidedHelpStoredTourProgress {
  version: string;
  status: Exclude<GuidedHelpTourProgressStatus, 'not_started'>;
  steps: Record<string, GuidedHelpStoredStepProgress>;
  lastSeenAt?: string;
  completedAt?: string;
  skippedAt?: string;
}

export interface GuidedHelpProgressState {
  schemaVersion: 1;
  updatedAt: string;
  skippedAll: boolean;
  skippedAllAt?: string;
  tours: Record<string, GuidedHelpStoredTourProgress>;
}

export interface GuidedHelpTourSummary {
  tourId: string;
  title: string;
  surface: GuidedHelpSurface;
  version: string;
  status: GuidedHelpTourProgressStatus;
  blockedBySkipAll: boolean;
  completedSteps: number;
  skippedSteps: number;
  viewedSteps: number;
  totalSteps: number;
  versionChanged: boolean;
}

export interface GuidedHelpResolvedStep {
  tour: GuidedHelpTour;
  step: GuidedHelpStep;
  stepIndex: number;
  totalSteps: number;
}

export interface GuidedHelpStorage {
  read: () => GuidedHelpProgressState;
  markStepViewed: (tour: GuidedHelpTour, step: GuidedHelpStep) => GuidedHelpProgressState;
  completeStep: (tour: GuidedHelpTour, step: GuidedHelpStep) => GuidedHelpProgressState;
  skipStep: (tour: GuidedHelpTour, step: GuidedHelpStep) => GuidedHelpProgressState;
  skipAll: () => GuidedHelpProgressState;
  undoSkipAll: () => GuidedHelpProgressState;
  resetTour: (tourId: string) => GuidedHelpProgressState;
  resetAllTours: () => GuidedHelpProgressState;
  replayTour: (tour: GuidedHelpTour) => GuidedHelpProgressState;
  getSummaries: (tours: GuidedHelpTour[]) => GuidedHelpTourSummary[];
}

export interface GuidedHelpSuppressWhen {
  termsOpen?: boolean;
  onboardingOpen?: boolean;
  metricsPromptOpen?: boolean;
  modalStackActive?: boolean;
  analyticsOpen?: boolean;
  kgHealthOpen?: boolean;
}

export interface GuidedHelpTelemetryEvent {
  action: 'viewed' | 'step_completed' | 'skipped_step' | 'skipped_all' | 'completed' | 'replayed' | 'reset';
  tour_surface: GuidedHelpSurface;
  step_kind: GuidedHelpStepKind;
  status: 'success' | 'skipped' | 'disabled' | 'fallback';
  duration_ms?: number;
}

export interface GuidedHelpTelemetryAdapter {
  emit: (event: GuidedHelpTelemetryEvent) => void | Promise<void>;
}

export interface GuidedHelpProviderProps {
  children: ReactNode;
  registry: GuidedHelpRegistry;
  surface: GuidedHelpSurface;
  suppressWhen?: GuidedHelpSuppressWhen;
  storage?: GuidedHelpStorage;
  telemetryAdapter?: GuidedHelpTelemetryAdapter;
  enabled?: boolean;
}

export interface GuidedHelpPopoverProgress {
  current: number;
  total: number;
}

export interface GuidedHelpPopoverProps {
  step: GuidedHelpStep;
  anchorRect: DOMRect | null;
  placement?: GuidedHelpPlacement;
  progress: GuidedHelpPopoverProgress;
  canGoBack?: boolean;
  onBack: () => void;
  onNext: () => void;
  onDone: () => void;
  onSkipStep: () => void;
  onSkipAll: () => void;
}

export interface GuidedHelpContextValue {
  activeTour: GuidedHelpTour | null;
  activeStep: GuidedHelpStep | null;
  activeStepIndex: number;
  activeTotalSteps: number;
  surface: GuidedHelpSurface;
  isSuppressed: boolean;
  skippedAll: boolean;
  progress: GuidedHelpProgressState;
  summaries: GuidedHelpTourSummary[];
  startTour: (tourId: string) => void;
  completeStep: (tourId?: string, stepId?: string) => void;
  skipStep: (tourId?: string, stepId?: string) => void;
  skipAll: () => void;
  undoSkipAll: () => void;
  resetTour: (tourId: string) => void;
  resetAllTours: () => void;
  replayTour: (tourId: string) => void;
  refresh: () => void;
}

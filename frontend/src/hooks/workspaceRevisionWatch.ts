import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { WorkspaceRevisions, WorkspaceSubjectRef } from '@/types';

export type WorkspaceSyncState = 'connecting' | 'current' | 'offline' | 'unavailable' | 'access-lost';
export interface WorkspaceChange {
  subjects: WorkspaceSubjectRef[];
  authorityChanged: boolean;
  initial: boolean;
}
export interface WorkspaceSubscription {
  subjects: WorkspaceSubjectRef[];
  onChange: (change: WorkspaceChange) => void;
  onState: (state: WorkspaceSyncState) => void;
  onAccessLost: () => void;
}
type ReadRevisions = (boardId: string, subjects: WorkspaceSubjectRef[], signal: AbortSignal) => Promise<WorkspaceRevisions>;
const keyOf = (item: WorkspaceSubjectRef) => `${item.subject_type}:${item.subject_id}`;
const tokenOf = (token: WorkspaceRevisions['authority'] | null) => token === null ? 'missing' : `${token.incarnation}:${token.revision}`;

/** One scheduler per authenticated client/Board, shared by visible consumers. */
export class WorkspaceRevisionWatch {
  private subscriptions = new Set<WorkspaceSubscription>();
  private tokens = new Map<string, string>();
  private authority: string | undefined;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private request: AbortController | undefined;
  private generation = 0;
  private failures = 0;

  constructor(
    private boardId: string,
    private read: ReadRevisions,
    private onAccessLost?: (isCurrent: () => boolean) => void,
  ) {}

  subscribe(subscription: WorkspaceSubscription) {
    this.subscriptions.add(subscription);
    subscription.onState('connecting');
    if (this.subscriptions.size === 1) {
      document.addEventListener('visibilitychange', this.wake);
      window.addEventListener('focus', this.wake);
      window.addEventListener('online', this.wake);
      window.addEventListener('offline', this.wake);
    }
    this.wake();
    return () => {
      this.subscriptions.delete(subscription);
      if (this.subscriptions.size === 0) {
        this.cancel();
        document.removeEventListener('visibilitychange', this.wake);
        window.removeEventListener('focus', this.wake);
        window.removeEventListener('online', this.wake);
        window.removeEventListener('offline', this.wake);
        this.tokens.clear();
        this.authority = undefined;
        this.failures = 0;
      } else this.wake();
    };
  }

  private cancel() {
    this.generation += 1;
    clearTimeout(this.timer);
    this.timer = undefined;
    this.request?.abort();
    this.request = undefined;
  }

  private visible() {
    return document.visibilityState !== 'hidden' && navigator.onLine !== false;
  }

  private state(value: WorkspaceSyncState) {
    for (const subscriber of this.subscriptions) subscriber.onState(value);
  }

  private wake = () => {
    this.cancel();
    if (navigator.onLine === false) this.state('offline');
    if (this.subscriptions.size && this.visible()) this.schedule(0);
  };

  private schedule(delay: number) {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => { void this.poll(); }, delay);
  }

  private async poll() {
    if (!this.visible() || this.subscriptions.size === 0) return;
    const generation = this.generation;
    const controller = new AbortController();
    this.request = controller;
    const current = () => generation === this.generation && !controller.signal.aborted;
    const subjects = [...new Map([...this.subscriptions].flatMap(item =>
      item.subjects.map(subject => [keyOf(subject), subject] as const))).values()];
    const incoming = new Map<string, string>();
    const denied = new Set<string>();
    let authority: string | undefined;
    const readBatch = async (batch: WorkspaceSubjectRef[]): Promise<void> => {
      if (!current()) return;
      let result: WorkspaceRevisions;
      try {
        result = await this.read(this.boardId, batch, controller.signal);
      } catch (error) {
        if (!current()) return;
        // A scope denial rejects the whole batch, not necessarily every subject.
        // Keep ordinary polls batched; isolate only refused scopes. A 401 still
        // invalidates the authenticated session and is handled globally below.
        if (!(error instanceof AuthenticatedFetchError) || ![403, 404].includes(error.status)) throw error;
        if (batch.length === 1) {
          denied.add(keyOf(batch[0]));
          return;
        }
        const middle = Math.ceil(batch.length / 2);
        await readBatch(batch.slice(0, middle));
        await readBatch(batch.slice(middle));
        return;
      }
      if (!current()) return;
      if (result.board_id !== this.boardId || result.subjects.length !== batch.length
        || result.subjects.some((item, index) => keyOf(item) !== keyOf(batch[index]))) {
        throw new Error('Workspace revisions do not match the requested scope.');
      }
      const nextAuthority = tokenOf(result.authority);
      if (authority !== undefined && authority !== nextAuthority) {
        throw new Error('Workspace authority changed during the batch.');
      }
      authority = nextAuthority;
      for (const item of result.subjects) incoming.set(keyOf(item), tokenOf(item.token));
    };
    const affectedByDenial = (subscriber: WorkspaceSubscription) =>
      subscriber.subjects.some(subject => denied.has(keyOf(subject)));
    const publishDenials = () => {
      if (!current() || denied.size === 0) return;
      for (const key of denied) this.tokens.delete(key);
      for (const subscriber of this.subscriptions) {
        if (!current()) return;
        if (affectedByDenial(subscriber)) {
          subscriber.onState('access-lost');
          subscriber.onAccessLost();
        }
      }
      this.onAccessLost?.(current);
    };
    try {
      for (let start = 0; start < subjects.length; start += 50) {
        await readBatch(subjects.slice(start, start + 50));
      }
      if (!current()) return;
      const initial = this.authority === undefined;
      const authorityChanged = !initial && this.authority !== authority;
      const changed = new Set([...incoming].filter(([key, token]) => this.tokens.get(key) !== token).map(([key]) => key));
      this.tokens = incoming;
      this.authority = authority;
      this.failures = denied.size ? this.failures + 1 : 0;
      // Publish only after every chunk was verified; stale responses never notify.
      for (const subscriber of this.subscriptions) {
        if (!current()) return;
        if (affectedByDenial(subscriber)) continue;
        subscriber.onState('current');
        const affected = subscriber.subjects.filter(subject => authorityChanged || changed.has(keyOf(subject)));
        if (affected.length) subscriber.onChange({ subjects: affected, authorityChanged, initial });
      }
      publishDenials();
    } catch (error) {
      if (!current()) return;
      this.failures += 1;
      if (error instanceof AuthenticatedFetchError && [401, 403, 404].includes(error.status)) {
        this.tokens.clear();
        this.authority = undefined;
        this.state('access-lost');
        for (const subscriber of this.subscriptions) subscriber.onAccessLost();
        this.onAccessLost?.(current);
      } else {
        this.state(navigator.onLine === false ? 'offline' : 'unavailable');
        // A later technical failure must not conceal an already confirmed denial.
        publishDenials();
      }
    } finally {
      if (current()) {
        this.request = undefined;
        if (this.subscriptions.size && this.visible()) this.schedule(Math.min(30_000, 5_000 * 2 ** this.failures));
      }
    }
  }
}

/**
 * GuidelinesPanel - Two-tab modal for managing board + global guidelines
 */

import {
  useState,
  useEffect,
  useCallback,
  useMemo,
  useRef,
} from 'react';
import {
  X, Plus, Search, BookOpen, Unlink,
  Tag, Globe, FileText, Edit3, Eye, EyeOff,
  HelpCircle, ShieldCheck,
} from 'lucide-react';
import { useDashboardApi } from '@/services/api';
import { usePolicyGovernanceApi } from '@/services/policy-governance-api';
import { MarkdownContent } from '@/components/shared/MarkdownContent';
import toast from 'react-hot-toast';
import type {
  BoardGuidelineEntry,
  DefaultBoardConfigGuidelineRef,
  DefaultGuidelineCandidatesResponse,
  Guideline,
} from '@/types';
import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import { useDialogFocusTrap } from '@/hooks/useDialogFocusTrap';
import { usePermissions } from '@/hooks/usePermissions';
import { PolicyWaiverPanel } from '@/components/policy-compliance/PolicyWaiverPanel';
import { ContextualHelpLink } from '@/components/help';
import {
  GuidelineRevisionEditor,
  type AdoptedGuidelineRevision,
} from './GuidelineRevisionEditor';
import {
  currentDefaultGuidelineRefs,
  defaultGuidelineRefFromCandidate,
} from './defaultGuidelineRefs';
import { GuidelinePolicyTransfer } from './GuidelinePolicyTransfer';
import {
  GuidelineImpactDialog,
  type AdoptedGuidelineBindingAuthority,
} from './GuidelineImpactDialog';
import {
  guidelineImpactErrorMessage,
  isCompleteBoardGuidelineBindingAuthority,
  isGuidelineRevisionAuthorityForTarget,
  latestGuidelineRevisionTargetFromAuthority,
} from './guidelineImpactModel';
import type {
  GuidelineAdoptionResponse,
  GuidelineEnforcement,
} from '@/types/policy-governance';

interface GuidelinesPanelProps {
  boardId: string;
  onClose: () => void;
}

type Tab = 'board' | 'global' | 'waivers';
const GLOBAL_PAGE_SIZE = 50;

interface RevisionEditorSelection {
  guideline: Guideline;
  adoptedRevision?: AdoptedGuidelineRevision;
}

interface ImpactDialogSelection {
  guidelineId: string;
  guidelineTitle: string;
  targetRevisionId: string;
  targetSemanticVersion: string;
  adoptedBinding?: AdoptedGuidelineBindingAuthority;
  initialPriority: number;
  initialEnforcement: GuidelineEnforcement;
}

export function GuidelinesPanel({ boardId, onClose }: GuidelinesPanelProps) {
  const api = useDashboardApi();
  const policyApi = usePolicyGovernanceApi();
  const permissions = usePermissions(boardId);
  const policyAuthorityReady = (
    !permissions.isLoading
    && !permissions.error
    && !permissions.ownerReviewRequired
  );
  const canReadRevisions = (
    policyAuthorityReady
    && permissions.has('guidelines.revisions.read')
  );
  const canCreateRevisions = (
    policyAuthorityReady
    && permissions.has('guidelines.revisions.create')
  );
  const canOpenImpact = (
    canReadRevisions
    && permissions.has('guidelines.impact.preview')
  );
  const canManageAdoption = (
    policyAuthorityReady
    && permissions.has('guidelines.adoption.manage')
  );
  const canReadWaivers = (
    policyAuthorityReady
    && permissions.has('guidelines.waiver.read')
  );
  const [activeTab, setActiveTab] = useState<Tab>('global');
  const [revisionEditor, setRevisionEditor] =
    useState<RevisionEditorSelection | null>(null);
  const [impactDialog, setImpactDialog] =
    useState<ImpactDialogSelection | null>(null);
  const [impactOpeningId, setImpactOpeningId] = useState<string | null>(null);
  const impactOpeningRef = useRef<string | null>(null);
  const childDialogOpen = (
    revisionEditor !== null
    || impactDialog !== null
  );
  useEscapeToClose(onClose, {
    enabled: !childDialogOpen,
  });
  const focusTrap = useDialogFocusTrap(
    !childDialogOpen,
    '[data-guidelines-initial-focus]',
  );

  // Board tab state
  const [entries, setEntries] = useState<BoardGuidelineEntry[]>([]);
  const [boardLoading, setBoardLoading] = useState(true);
  const [showInlineForm, setShowInlineForm] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  // Global tab state
  const [globals, setGlobals] = useState<Guideline[]>([]);
  const [globalLoading, setGlobalLoading] = useState(false);
  const [globalLoadingMore, setGlobalLoadingMore] = useState(false);
  const [globalHasMore, setGlobalHasMore] = useState(false);
  const [globalSearch, setGlobalSearch] = useState('');
  const [showGlobalForm, setShowGlobalForm] = useState(false);

  // Form state (shared between inline create, global create, and edit)
  const [formTitle, setFormTitle] = useState('');
  const [formContent, setFormContent] = useState('');
  const [formTags, setFormTags] = useState('');
  const [showHelp, setShowHelp] = useState(false);

  // Guideline default state, derived from the umbrella template (spec 8a2fad91 /
  // card 5cb88511). Only GLOBAL catalog guidelines are eligible defaults; the
  // Set-default action is blocked for inline guidelines (FR5/AC7).
  const [defaultInfo, setDefaultInfo] = useState<DefaultGuidelineCandidatesResponse | null>(null);
  const [draftDefaultRefs, setDraftDefaultRefs] =
    useState<DefaultBoardConfigGuidelineRef[] | null>(null);
  const [savingDefaults, setSavingDefaults] = useState(false);
  const draftDefaultRefsRef =
    useRef<DefaultBoardConfigGuidelineRef[] | null>(null);

  const fetchDefaults = useCallback(async () => {
    try {
      const info = await api.listDefaultGuidelineCandidates();
      setDefaultInfo(info);
      return info;
    } catch {
      setDefaultInfo(null);
      setDraftDefaultRefs(null);
      return null;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (canReadRevisions) {
      void fetchDefaults();
    } else if (!permissions.isLoading) {
      setDefaultInfo(null);
      setDraftDefaultRefs(null);
    }
  }, [canReadRevisions, fetchDefaults, permissions.isLoading]);

  const baseDefaultRefs = useMemo(() => {
    try {
      return defaultInfo
        ? currentDefaultGuidelineRefs(defaultInfo.candidates)
        : null;
    } catch {
      return null;
    }
  }, [defaultInfo]);
  const effectiveDefaultRefs =
    draftDefaultRefs ?? baseDefaultRefs ?? [];
  draftDefaultRefsRef.current = draftDefaultRefs;
  const defaultRefsDirty = (
    draftDefaultRefs !== null
    && JSON.stringify(draftDefaultRefs)
      !== JSON.stringify(baseDefaultRefs ?? [])
  );
  const isGuidelineDefault = (guidelineId: string) =>
    effectiveDefaultRefs.some((ref) => ref.guideline_id === guidelineId);

  const toggleDefault = (guidelineId: string) => {
    if (!canManageAdoption) {
      toast.error('Guideline adoption permission is required to change defaults');
      return;
    }
    if (!defaultInfo) {
      toast.error('Default guideline authority is unavailable');
      return;
    }
    const candidate = defaultInfo.candidates.find(
      (item) => item.guideline_id === guidelineId,
    );
    if (!candidate) {
      toast.error('Guideline candidate is unavailable');
      return;
    }
    let current: DefaultBoardConfigGuidelineRef[];
    try {
      if (baseDefaultRefs === null) {
        throw new Error('default_guideline_refs_invalid');
      }
      current = effectiveDefaultRefs;
    } catch {
      toast.error('A persisted default revision pin is invalid');
      return;
    }
    const already = current.some((ref) => ref.guideline_id === guidelineId);
    if (!already && (!candidate.eligible || candidate.retired)) {
      toast.error('Retired guidelines cannot become new defaults');
      return;
    }
    if (
      !already
      && defaultInfo.candidates.some(
        (item) => (
          item.retired
          && current.some((ref) => ref.guideline_id === item.guideline_id)
        ),
      )
    ) {
      toast.error('Remove retired defaults before adding another guideline');
      return;
    }
    const refs = already
      ? current.filter((ref) => ref.guideline_id !== guidelineId)
      : [
          ...current,
          defaultGuidelineRefFromCandidate(
            candidate,
            'head',
            current.reduce(
              (maximum, ref) => Math.max(maximum, ref.priority),
              0,
            ) + 1,
          ),
        ];
    setDraftDefaultRefs(refs);
  };

  const stageLatestDefaultRevision = (guidelineId: string) => {
    if (!canManageAdoption) {
      toast.error('Guideline adoption permission is required to change defaults');
      return;
    }
    if (!defaultInfo || baseDefaultRefs === null) {
      toast.error('Default guideline authority is unavailable');
      return;
    }
    const candidate = defaultInfo.candidates.find(
      (item) => item.guideline_id === guidelineId,
    );
    const current = effectiveDefaultRefs;
    const existing = current.find(
      (ref) => ref.guideline_id === guidelineId,
    );
    if (!candidate || !existing || !candidate.eligible || candidate.retired) {
      toast.error('This guideline cannot be pinned to its latest revision');
      return;
    }
    setDraftDefaultRefs(
      current.map((ref) => (
        ref.guideline_id === guidelineId
          ? defaultGuidelineRefFromCandidate(
              candidate,
              'head',
              ref.priority,
            )
          : ref
      )),
    );
  };

  const saveDefaultChanges = async () => {
    const refs = draftDefaultRefsRef.current;
    if (
      !canManageAdoption
      || !defaultInfo
      || refs === null
      || savingDefaults
    ) return;
    setSavingDefaults(true);
    try {
      if (!defaultInfo.template_id) {
        await api.createDefaultBoardConfigVersion({
          guideline_default_refs: refs,
          activate: true,
        });
      } else {
        await api.updateDefaultGuidelineRefs(
          defaultInfo.template_id,
          refs,
        );
      }
      toast.success('Guideline defaults saved as a new template version');
      setDraftDefaultRefs(null);
      await fetchDefaults();
    } catch {
      toast.error('Failed to save guideline defaults');
    } finally {
      setSavingDefaults(false);
    }
  };

  const resetForm = () => { setFormTitle(''); setFormContent(''); setFormTags(''); };

  const parseTags = () => formTags.split(',').map(t => t.trim()).filter(Boolean);

  // ==================== BOARD TAB ====================

  const fetchBoard = useCallback(async () => {
    try {
      setBoardLoading(true);
      const data = await api.getBoardGuidelines(boardId);
      setEntries(data.sort((a, b) => a.priority - b.priority));
    } catch { toast.error('Failed to load board guidelines'); }
    finally { setBoardLoading(false); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boardId]);

  useEffect(() => {
    if (canReadRevisions) {
      void fetchBoard();
    } else if (!permissions.isLoading) {
      setEntries([]);
      setBoardLoading(false);
    }
  }, [canReadRevisions, fetchBoard, permissions.isLoading]);

  const handleUnlink = async (entry: BoardGuidelineEntry) => {
    if (!canManageAdoption) {
      toast.error('Guideline adoption permission is required to unlink');
      return;
    }
    try {
      await api.unlinkGuidelineFromBoard(boardId, entry.guideline.id);
      toast.success('Guideline removed from board');
      fetchBoard();
    } catch { toast.error('Failed to remove'); }
  };

  const handleCreateInline = async () => {
    if (!canCreateRevisions) {
      toast.error('Guideline revision creation permission is required');
      return;
    }
    if (!formTitle.trim() || !formContent.trim()) { toast.error('Title and content required'); return; }
    try {
      const tags = parseTags();
      await api.createInlineGuideline(boardId, { title: formTitle.trim(), content: formContent.trim(), tags: tags.length ? tags : undefined });
      toast.success('Inline guideline created');
      resetForm();
      setShowInlineForm(false);
      fetchBoard();
    } catch { toast.error('Failed to create'); }
  };

  const handleUnlinkByGuidelineId = async (guidelineId: string) => {
    if (!canManageAdoption) {
      toast.error('Guideline adoption permission is required to unlink');
      return;
    }
    try {
      await api.unlinkGuidelineFromBoard(boardId, guidelineId);
      toast.success('Guideline removed from board');
      fetchBoard();
    } catch { toast.error('Failed to remove'); }
  };

  // ==================== GLOBAL TAB ====================

  const fetchGlobals = useCallback(async (offset = 0) => {
    const loadingFirstPage = offset === 0;
    try {
      if (loadingFirstPage) setGlobalLoading(true);
      else setGlobalLoadingMore(true);
      const page = await api.listGuidelines(offset, GLOBAL_PAGE_SIZE);
      const globalPage = page.filter((guideline) => guideline.scope === 'global');
      setGlobals((current) => {
        const base = loadingFirstPage ? [] : current;
        const existing = new Set(base.map((guideline) => guideline.id));
        return [
          ...base,
          ...globalPage.filter((guideline) => !existing.has(guideline.id)),
        ];
      });
      setGlobalHasMore(page.length === GLOBAL_PAGE_SIZE);
    } catch { toast.error('Failed to load global guidelines'); }
    finally {
      setGlobalLoading(false);
      setGlobalLoadingMore(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (activeTab === 'global' && canReadRevisions) void fetchGlobals(0);
  }, [activeTab, canReadRevisions, fetchGlobals]);

  const handleCreateGlobal = async () => {
    if (!canCreateRevisions) {
      toast.error('Guideline revision creation permission is required');
      return;
    }
    if (!formTitle.trim() || !formContent.trim()) { toast.error('Title and content required'); return; }
    try {
      const tags = parseTags();
      await api.createGuideline({ title: formTitle.trim(), content: formContent.trim(), tags: tags.length ? tags : undefined, scope: 'global' });
      toast.success('Global guideline created');
      resetForm();
      setShowGlobalForm(false);
      await Promise.all([fetchGlobals(0), fetchDefaults()]);
    } catch { toast.error('Failed to create'); }
  };

  const openRevisionEditor = (
    guideline: Guideline,
    entry?: BoardGuidelineEntry,
  ) => {
    if (!canReadRevisions) {
      toast.error('Guideline revision read permission is required');
      return;
    }
    setRevisionEditor({
      guideline,
      ...(entry?.guideline.semantic_version
        ? {
            adoptedRevision: {
              semanticVersion: entry.guideline.semantic_version,
              revisionId: entry.guideline.revision_id,
              bindingRevision: entry.binding_revision,
            },
          }
        : {}),
    });
  };

  useEffect(() => {
    if (permissions.isLoading) return;
    if (activeTab === 'waivers' && !canReadWaivers && canReadRevisions) {
      setActiveTab('global');
    } else if (
      (activeTab === 'global' || activeTab === 'board')
      && !canReadRevisions
      && canReadWaivers
    ) {
      setActiveTab('waivers');
    }
  }, [
    activeTab,
    canReadRevisions,
    canReadWaivers,
    permissions.isLoading,
  ]);

  const openImpactPreview = async (
    guideline: Guideline,
    entry?: BoardGuidelineEntry,
  ) => {
    if (!canOpenImpact || impactOpeningRef.current !== null) return;
    const candidate = defaultInfo?.candidates.find(
      (item) => item.guideline_id === guideline.id,
    );
    if (entry && !isCompleteBoardGuidelineBindingAuthority(entry)) {
      toast.error(
        'The current binding authority is incomplete. Reload before adoption.',
      );
      return;
    }
    impactOpeningRef.current = guideline.id;
    setImpactOpeningId(guideline.id);
    try {
      let targetRevisionId: string;
      let targetSemanticVersion: string;
      if (candidate) {
        if (!candidate.eligible || candidate.retired) {
          throw new Error(
            'The latest guideline revision is unavailable for adoption.',
          );
        }
        targetRevisionId = candidate.head_revision.revision_id;
        targetSemanticVersion = candidate.head_revision.semantic_version;
      } else if (entry?.guideline.revision_id) {
        const adoptedAuthority = await policyApi.getGuidelineRevision(
          boardId,
          guideline.id,
          entry.guideline.revision_id,
        );
        const latest = latestGuidelineRevisionTargetFromAuthority(
          adoptedAuthority,
          {
            guidelineId: guideline.id,
            requestedRevisionId: entry.guideline.revision_id,
          },
        );
        if (!latest) {
          throw new Error(
            'The latest guideline revision authority is malformed or retired.',
          );
        }
        const latestAuthority = await policyApi.getGuidelineRevision(
          boardId,
          guideline.id,
          latest.revisionId,
        );
        if (!isGuidelineRevisionAuthorityForTarget(latestAuthority, {
          guidelineId: guideline.id,
          revisionId: latest.revisionId,
          semanticVersion: latest.semanticVersion,
        })) {
          throw new Error(
            'The latest guideline revision authority changed during discovery.',
          );
        }
        targetRevisionId = latest.revisionId;
        targetSemanticVersion = latest.semanticVersion;
      } else {
        throw new Error(
          'The latest guideline revision is unavailable for adoption.',
        );
      }
      const nextPriority = entries.reduce(
        (maximum, current) => Math.max(maximum, current.priority),
        -1,
      ) + 1;
      setImpactDialog({
        guidelineId: guideline.id,
        guidelineTitle: guideline.title,
        targetRevisionId,
        targetSemanticVersion,
        ...(entry && isCompleteBoardGuidelineBindingAuthority(entry)
          ? {
              adoptedBinding: {
                bindingId: entry.binding_id,
                bindingRevision: entry.binding_revision,
                bindingState: entry.binding_state,
                revisionId: entry.guideline.revision_id,
                semanticVersion: entry.guideline.semantic_version,
                revisionDigest: entry.guideline.revision_digest,
              },
            }
          : {}),
        initialPriority: entry?.priority ?? nextPriority,
        initialEnforcement: entry?.default_enforcement ?? 'advisory',
      });
    } catch (error: unknown) {
      toast.error(guidelineImpactErrorMessage(error));
    } finally {
      if (impactOpeningRef.current === guideline.id) {
        impactOpeningRef.current = null;
        setImpactOpeningId(null);
      }
    }
  };

  const adoptionUpdateAvailable = (
    candidate: DefaultGuidelineCandidatesResponse['candidates'][number] | undefined,
    entry: BoardGuidelineEntry | undefined,
  ) => {
    if (!candidate || !entry) return false;
    if (entry.guideline.revision_id) {
      return (
        entry.guideline.revision_id
        !== candidate.head_revision.revision_id
      );
    }
    return (
      entry.guideline.semantic_version
      !== candidate.head_revision.semantic_version
    );
  };

  const refreshPolicyUi = async () => {
    if (!canReadRevisions) return;
    await Promise.all([fetchBoard(), fetchGlobals(0), fetchDefaults()]);
  };

  const handleAdopted = async (
    _response: GuidelineAdoptionResponse,
  ) => {
    toast.success('Guideline revision adopted');
    await refreshPolicyUi();
  };

  const filteredGlobals = globals.filter(g =>
    !globalSearch || g.title.toLowerCase().includes(globalSearch.toLowerCase())
  );
  const successorOptions = (defaultInfo?.candidates ?? [])
    .filter(
      (candidate) =>
        candidate.scope === 'global'
        && !candidate.retired
        && candidate.eligible
        && candidate.guideline_id !== revisionEditor?.guideline.id,
    )
    .map((candidate) => ({
      guidelineId: candidate.guideline_id,
      title: candidate.title,
      semanticVersion: candidate.semantic_version,
    }));

  // ==================== SHARED RENDERERS ====================

  const tagBadges = (tags: string[] | null) => {
    if (!tags?.length) return null;
    return (
      <div className="flex flex-wrap gap-1 mt-1">
        {tags.map(t => <span key={t} className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-medium bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300"><Tag size={8} />{t}</span>)}
      </div>
    );
  };

  const guidelineForm = (onSave: () => void, onCancel: () => void, saveLabel: string) => (
    <div className="space-y-3 border border-gray-200 dark:border-gray-700 rounded-lg p-4 bg-white dark:bg-gray-800">
      <input type="text" value={formTitle} onChange={e => setFormTitle(e.target.value)} placeholder="Guideline title" className="w-full px-3 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-900 text-gray-900 dark:text-white outline-none focus:ring-2 focus:ring-blue-300" />
      <textarea value={formContent} onChange={e => setFormContent(e.target.value)} rows={8} placeholder="Content (Markdown supported)" className="w-full px-3 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-900 text-gray-900 dark:text-white outline-none focus:ring-2 focus:ring-blue-300 font-mono" />
      <input type="text" value={formTags} onChange={e => setFormTags(e.target.value)} placeholder="Tags (comma-separated)" className="w-full px-3 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-900 text-gray-900 dark:text-white outline-none focus:ring-2 focus:ring-blue-300" />
      <div className="flex gap-2">
        <button onClick={onSave} className="btn btn-primary text-sm">{saveLabel}</button>
        <button onClick={onCancel} className="btn btn-secondary text-sm">Cancel</button>
      </div>
    </div>
  );

  const helpPanel = showHelp && (
    <section
      data-testid="guideline-help-examples"
      className="mb-4 rounded-lg border border-blue-200 bg-blue-50 p-4 text-sm text-blue-950 dark:border-blue-500/30 dark:bg-blue-500/10 dark:text-blue-100"
    >
      <div className="mb-2 flex items-center gap-2 font-semibold">
        <HelpCircle size={15} />
        Assistant context examples
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        <div>
          <div className="text-xs font-semibold uppercase opacity-75">Board workflow</div>
          <p className="mt-1 text-xs leading-5">
            Agents must read board guidelines before moving entities, request validator review at every review gate,
            and document blockers as comments with the responsible owner mentioned.
          </p>
        </div>
        <div>
          <div className="text-xs font-semibold uppercase opacity-75">Engineering policy</div>
          <p className="mt-1 text-xs leading-5">
            Preserve existing architecture, keep changes scoped to the card objective, and run focused tests before validation.
          </p>
        </div>
        <div>
          <div className="text-xs font-semibold uppercase opacity-75">Ownership</div>
          <p className="mt-1 text-xs leading-5">
            Use guidelines for repo boundaries, agent responsibilities, approval roles, and board-specific escalation rules.
          </p>
        </div>
        <div>
          <div className="text-xs font-semibold uppercase opacity-75">Default usage</div>
          <p className="mt-1 text-xs leading-5">
            Mark global catalog guidelines as default when every new board should inherit that assistant context.
          </p>
        </div>
      </div>
      <div className="mt-3 border-t border-blue-200 pt-3 dark:border-blue-500/30">
        <ContextualHelpLink
          sectionId="policy-governance"
          testId="guideline-policy-governance-help"
        >
          Open the Policy Governance guide
        </ContextualHelpLink>
      </div>
    </section>
  );

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div
        ref={focusTrap.dialogRef}
        role="dialog"
        aria-modal={
          !childDialogOpen
            ? 'true'
            : undefined
        }
        aria-hidden={
          childDialogOpen
            ? 'true'
            : undefined
        }
        aria-labelledby="guidelines-panel-title"
        tabIndex={-1}
        onKeyDown={focusTrap.onKeyDown}
        className="flex h-[90vh] w-full max-w-5xl flex-col overflow-hidden rounded-lg border border-gray-200 bg-white shadow-2xl dark:border-gray-700 dark:bg-gray-900"
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700 shrink-0">
          <div className="flex items-center gap-2">
            <BookOpen size={20} className="text-blue-500" />
            <h2 id="guidelines-panel-title" className="text-lg font-semibold text-gray-900 dark:text-white">Guidelines</h2>
          </div>
          <div className="flex items-center gap-2">
            {(canReadRevisions || canCreateRevisions) && (
              <GuidelinePolicyTransfer
                boardId={boardId}
                onImported={refreshPolicyUi}
              />
            )}
            <ContextualHelpLink
              sectionId="policy-governance"
              testId="guidelines-contextual-help"
            >
              Policy guide
            </ContextualHelpLink>
            <button
              type="button"
              onClick={() => setShowHelp((value) => !value)}
              data-testid="guideline-help-toggle"
              className="inline-flex items-center gap-1 rounded-md border border-gray-300 px-2.5 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-50 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800"
            >
              <HelpCircle size={14} />
              Examples
            </button>
            <button
              type="button"
              data-guidelines-initial-focus
              aria-label="Close guidelines"
              onClick={onClose}
              className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg"
            >
              <X size={18} />
            </button>
          </div>
        </div>

        <div className="grid min-h-0 flex-1 grid-cols-[240px_minmax(0,1fr)]">
          <aside className="border-r border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-gray-950/30">
            <nav className="space-y-1 text-sm">
              {([
                ...(canReadRevisions
                  ? [
                      { id: 'global' as Tab, label: 'Global Catalog', icon: <Globe size={14} />, count: globals.length || defaultInfo?.candidates?.length || 0 },
                      { id: 'board' as Tab, label: 'Board Guidelines', icon: <FileText size={14} />, count: entries.length },
                    ]
                  : []),
                ...(canReadWaivers
                  ? [{
                      id: 'waivers' as Tab,
                      label: 'Waivers',
                      icon: <ShieldCheck size={14} />,
                      count: null,
                    }]
                  : []),
              ]).map(tab => (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  className={`flex w-full items-center justify-between gap-2 rounded-md px-3 py-2 font-medium transition-colors ${
                    activeTab === tab.id
                      ? 'bg-white text-gray-900 shadow-sm ring-1 ring-gray-200 dark:bg-gray-800 dark:text-white dark:ring-gray-700'
                      : 'text-gray-600 hover:bg-white/70 dark:text-gray-400 dark:hover:bg-gray-800/60'
                  }`}
                >
                  <span className="flex min-w-0 items-center gap-2">
                    {tab.icon}
                    <span className="truncate">{tab.label}</span>
                  </span>
                  {tab.count !== null && (
                    <span className="shrink-0 rounded bg-gray-200 px-1.5 py-0.5 text-[10px] text-gray-600 dark:bg-gray-700 dark:text-gray-300">
                      {tab.count}
                    </span>
                  )}
                </button>
              ))}
              {canReadRevisions && (
              <div className="mt-3 rounded-md border border-gray-200 bg-white px-3 py-2 text-xs text-gray-500 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-400">
                <div className="font-medium text-gray-700 dark:text-gray-200">Default template</div>
                <div className="mt-0.5">
                  {defaultInfo?.template_version ? `v${defaultInfo.template_version}` : 'No active template'}
                </div>
                <p className="mt-1 text-[10px] leading-4">
                  Changes are staged and affect only boards created from the next saved template version.
                </p>
                {defaultRefsDirty && (
                  <div
                    className="mt-3 space-y-2 border-t border-gray-200 pt-2 dark:border-gray-700"
                    data-testid="guideline-default-draft"
                  >
                    <p className="font-medium text-amber-700 dark:text-amber-300">
                      Unsaved default changes
                    </p>
                    <div className="flex gap-1">
                      <button
                        type="button"
                        disabled={savingDefaults || !canManageAdoption}
                        onClick={() => void saveDefaultChanges()}
                        data-testid="guideline-default-save"
                        className="rounded bg-blue-600 px-2 py-1 text-[10px] font-semibold text-white disabled:opacity-40"
                      >
                        {savingDefaults ? 'Saving…' : 'Save defaults'}
                      </button>
                      <button
                        type="button"
                        disabled={savingDefaults}
                        onClick={() => setDraftDefaultRefs(null)}
                        data-testid="guideline-default-discard"
                        className="rounded border border-gray-300 px-2 py-1 text-[10px] font-medium dark:border-gray-700"
                      >
                        Discard
                      </button>
                    </div>
                  </div>
                )}
              </div>
              )}
            </nav>
          </aside>

          {/* Body */}
          <main className="min-w-0 flex-1 overflow-y-auto p-6">
          {helpPanel}
          {!permissions.isLoading
            && !canReadRevisions
            && !canReadWaivers && (
            <section
              role="alert"
              data-testid="guidelines-authority-unavailable"
              className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300"
            >
              {canCreateRevisions
                ? 'No readable guideline view is granted. Authorized policy imports remain available above.'
                : 'Guideline governance is unavailable because current board authority could not be verified or no applicable capability is granted.'}
            </section>
          )}

          {/* ==================== BOARD TAB ==================== */}
          {activeTab === 'board' && canReadRevisions && (
            <div className="space-y-4">
              {/* Actions */}
              <div className="flex items-center gap-2">
                {canCreateRevisions && (
                  <button
                    type="button"
                    onClick={() => { resetForm(); setShowInlineForm(!showInlineForm); }}
                    className="btn btn-secondary flex items-center gap-1 text-sm"
                  >
                    <Plus size={14} /> Create Inline
                  </button>
                )}
              </div>

              {/* Inline create form */}
              {canCreateRevisions
                && showInlineForm
                && guidelineForm(handleCreateInline, () => setShowInlineForm(false), 'Create Inline')}

              {/* Guidelines list */}
              {boardLoading ? (
                <div className="text-center py-8 text-gray-400">Loading...</div>
              ) : entries.length === 0 && !showInlineForm ? (
                <div className="text-center py-12 text-gray-400">
                  <BookOpen size={36} className="mx-auto mb-2 opacity-40" />
                  <p className="text-sm">No guidelines on this board</p>
                  <p className="text-xs mt-1">Use Global Catalog to link a global guideline, or create an inline one</p>
                </div>
              ) : (
                <div className="space-y-2">
                  {entries.map((entry) => {
                    const isGlobal = entry.scope === 'global';
                    const isExpanded = expandedId === entry.id;
                    const candidate = defaultInfo?.candidates.find(
                      (item) => item.guideline_id === entry.guideline.id,
                    );
                    const hasAdoptionUpdate = adoptionUpdateAvailable(
                      candidate,
                      entry,
                    );
                    const bindingAuthorityComplete =
                      isCompleteBoardGuidelineBindingAuthority(entry);
                    return (
                      <div
                        key={entry.id}
                        className={`rounded-lg overflow-hidden border ${
                          isGlobal
                            ? 'border-blue-200 dark:border-blue-800 bg-blue-50/30 dark:bg-blue-900/10'
                            : 'border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50'
                        }`}
                      >
                        {/* Header row */}
                        <div className="flex items-center gap-2 px-3 py-2.5">
                          <button
                            type="button"
                            aria-expanded={isExpanded}
                            aria-controls={`guideline-details-${entry.id}`}
                            onClick={() => setExpandedId(
                              isExpanded ? null : entry.id,
                            )}
                            data-testid={`guideline-expand-${entry.guideline.id}`}
                            className="flex min-w-0 flex-1 items-center gap-2 text-left"
                          >
                            {/* Priority is policy-governed. It is edited only
                                through impact preview + explicit adoption. */}
                            <span
                              className="shrink-0 rounded bg-gray-100 px-1.5 py-0.5 font-mono text-[9px] text-gray-500 dark:bg-gray-800 dark:text-gray-300"
                              title="Binding priority; change through impact preview"
                              data-testid={`guideline-priority-${entry.guideline.id}`}
                            >
                              p{entry.priority}
                            </span>

                            {isGlobal ? (
                              <Globe size={14} className="text-blue-500 shrink-0" />
                            ) : (
                              <FileText size={14} className="text-gray-400 shrink-0" />
                            )}
                            <h3 className="text-sm font-medium text-gray-900 dark:text-white truncate flex-1">
                              {entry.guideline.title}
                            </h3>
                            <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium shrink-0 ${
                              isGlobal ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300' : 'bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400'
                            }`}>
                              {isGlobal ? 'Global' : 'Inline'}
                            </span>
                            <span className="shrink-0 text-[10px] text-gray-500 dark:text-gray-400">
                              Adopted v{entry.guideline.semantic_version ?? '—'}
                              {' · '}
                              {isGlobal
                                ? `Latest v${candidate?.head_revision.semantic_version ?? '—'}`
                                : 'Latest checked on review'}
                            </span>
                            {hasAdoptionUpdate && (
                              <span className="shrink-0 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-500/15 dark:text-amber-200">
                                Update available
                              </span>
                            )}
                            {isExpanded ? (
                              <EyeOff size={12} className="text-gray-400 shrink-0" />
                            ) : (
                              <Eye size={12} className="text-gray-400 shrink-0" />
                            )}
                          </button>

                          {/* Default state + Set-default action (blocked for inline, FR5/AC7) */}
                          <button
                            type="button"
                            onClick={() => toggleDefault(entry.guideline.id)}
                            disabled={
                              !isGlobal
                              || !canManageAdoption
                              || savingDefaults
                              || !defaultInfo
                              || baseDefaultRefs === null
                            }
                            title={isGlobal ? 'Toggle as a global default for new boards' : 'Inline guidelines cannot be defaults'}
                            data-testid={`guideline-set-default-${entry.guideline.id}`}
                            className={`text-[10px] px-1.5 py-0.5 rounded border shrink-0 disabled:opacity-40 disabled:cursor-not-allowed ${
                              isGuidelineDefault(entry.guideline.id)
                                ? 'bg-blue-500 text-white border-blue-500'
                                : 'text-gray-500 border-gray-300 dark:border-gray-600'
                            }`}
                          >
                            {isGuidelineDefault(entry.guideline.id) ? 'Default ✓' : 'Set default'}
                          </button>
                        </div>

                        {/* Expanded content */}
                        {isExpanded && (
                          <div
                            id={`guideline-details-${entry.id}`}
                            className="px-4 pb-3 border-t border-gray-100 dark:border-gray-700/50 pt-2"
                          >
                            <MarkdownContent content={entry.guideline.content} />
                            {tagBadges(entry.guideline.tags)}
                            <div className="flex items-center gap-1 mt-3 pt-2 border-t border-gray-100 dark:border-gray-700/50">
                              {canReadRevisions && (
                                <>
                                  <button
                                    type="button"
                                    disabled={
                                      !canOpenImpact
                                      || !bindingAuthorityComplete
                                      || impactOpeningId !== null
                                    }
                                    onClick={() => void openImpactPreview(
                                      entry.guideline,
                                      entry,
                                    )}
                                    data-testid={`guideline-review-adoption-${entry.guideline.id}`}
                                    className="flex items-center gap-1 text-xs font-medium text-blue-600 hover:text-blue-700 disabled:cursor-not-allowed disabled:opacity-40 dark:text-blue-300"
                                  >
                                    <ShieldCheck size={11} />
                                    {impactOpeningId === entry.guideline.id
                                      ? 'Loading latest…'
                                      : hasAdoptionUpdate
                                        ? 'Review update'
                                        : 'Review binding'}
                                  </button>
                                  <button
                                    type="button"
                                    disabled={!canManageAdoption}
                                    onClick={() => void handleUnlink(entry)}
                                    className="flex items-center gap-1 text-xs text-orange-500 hover:text-orange-600 disabled:cursor-not-allowed disabled:opacity-40"
                                  >
                                    <Unlink size={11} /> Unlink from board
                                  </button>
                                  <button
                                    type="button"
                                    onClick={() => openRevisionEditor(entry.guideline, entry)}
                                    className="ml-auto flex items-center gap-1 text-xs text-blue-600 hover:text-blue-700 dark:text-blue-300"
                                  >
                                    <Edit3 size={11} />
                                    Open revision editor
                                  </button>
                                </>
                              )}
                            </div>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}

          {/* ==================== GLOBAL TAB ==================== */}
          {activeTab === 'global' && canReadRevisions && (
            <div className="space-y-4">
              {/* Actions */}
              <div className="flex items-center gap-2">
                {canCreateRevisions && (
                  <button
                    type="button"
                    onClick={() => { resetForm(); setShowGlobalForm(!showGlobalForm); }}
                    className="btn btn-primary flex items-center gap-1 text-sm"
                  >
                    <Plus size={14} /> New Global Guideline
                  </button>
                )}
                <div className="relative flex-1">
                  <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
                  <input type="text" value={globalSearch} onChange={e => setGlobalSearch(e.target.value)} placeholder="Search..." className="w-full pl-9 pr-3 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-white outline-none" />
                </div>
              </div>

              {/* Create form */}
              {canCreateRevisions
                && showGlobalForm
                && guidelineForm(handleCreateGlobal, () => setShowGlobalForm(false), 'Create Global')}

              {/* List */}
              {globalLoading ? (
                <div className="text-center py-8 text-gray-400">Loading...</div>
              ) : filteredGlobals.length === 0 ? (
                <div className="text-center py-12 text-gray-400">
                  <Globe size={36} className="mx-auto mb-2 opacity-40" />
                  <p className="text-sm">{globalSearch ? 'No matching guidelines' : 'No global guidelines yet'}</p>
                </div>
              ) : (
                <div className="space-y-2">
                  {filteredGlobals.map(g => {
                    const boardEntry = entries.find(e => e.guideline.id === g.id);
                    const linkedToBoard = Boolean(boardEntry);
                    const isDefault = isGuidelineDefault(g.id);
                    const candidate = defaultInfo?.candidates.find(
                      (item) => item.guideline_id === g.id,
                    );
                    const defaultRef = effectiveDefaultRefs.find(
                      (ref) => ref.guideline_id === g.id,
                    );
                    const defaultUpdateAvailable = Boolean(
                      defaultRef
                      && candidate
                      && (
                        defaultRef.revision_id
                        !== candidate.head_revision.revision_id
                      ),
                    );
                    const boardUpdateAvailable = adoptionUpdateAvailable(
                      candidate,
                      boardEntry,
                    );
                    const bindingAuthorityComplete = (
                      !boardEntry
                      || isCompleteBoardGuidelineBindingAuthority(boardEntry)
                    );
                    return (
                      <div key={g.id} className="border border-gray-200 dark:border-gray-700 rounded-lg p-3 bg-white dark:bg-gray-800/50">
                        <div className="flex items-start justify-between gap-3">
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-2 mb-1">
                              <h3 className="text-sm font-medium text-gray-900 dark:text-white truncate">{g.title}</h3>
                              <span className="text-[10px] text-gray-500 dark:text-gray-400 shrink-0">
                                Adopted {boardEntry?.guideline.semantic_version
                                  ? `v${boardEntry.guideline.semantic_version}`
                                  : '—'}
                              </span>
                              <span className="text-[10px] text-gray-500 dark:text-gray-400 shrink-0">
                                Latest v{candidate?.head_revision.semantic_version ?? g.semantic_version ?? '—'}
                              </span>
                              {linkedToBoard && <span className="text-[10px] px-1 py-0.5 rounded bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300 shrink-0">linked</span>}
                              {boardUpdateAvailable && <span className="text-[10px] px-1 py-0.5 rounded bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300 shrink-0">update available</span>}
                              {isDefault && (
                                <span className="text-[10px] px-1 py-0.5 rounded bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300 shrink-0">
                                  default v{defaultRef?.semantic_version ?? '—'}
                                </span>
                              )}
                              {defaultUpdateAvailable && <span className="text-[10px] px-1 py-0.5 rounded bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300 shrink-0">default update available</span>}
                              {candidate?.retired && <span className="text-[10px] px-1 py-0.5 rounded bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300 shrink-0">retired</span>}
                            </div>
                            <p className="text-xs text-gray-500 dark:text-gray-400 line-clamp-2">{g.content.slice(0, 150)}{g.content.length > 150 ? '...' : ''}</p>
                            {tagBadges(g.tags)}
                          </div>
                          <div className="flex items-center gap-1 shrink-0">
                            {linkedToBoard ? (
                              <>
                                <button
                                  type="button"
                                  disabled={
                                    !canOpenImpact
                                    || !bindingAuthorityComplete
                                    || impactOpeningId !== null
                                  }
                                  onClick={() => void openImpactPreview(
                                    g,
                                    boardEntry,
                                  )}
                                  className="inline-flex items-center gap-1 rounded border border-blue-200 px-2 py-1 text-[10px] font-medium text-blue-600 hover:bg-blue-50 disabled:cursor-not-allowed disabled:opacity-40 dark:border-blue-800 dark:text-blue-300 dark:hover:bg-blue-900/20"
                                  title="Review a persisted impact receipt before changing this binding"
                                  data-testid={`guideline-adopt-board-${g.id}`}
                                >
                                  <ShieldCheck size={11} />
                                  {impactOpeningId === g.id
                                    ? 'Loading latest…'
                                    : boardUpdateAvailable
                                      ? 'Review update'
                                      : 'Review binding'}
                                </button>
                                <button
                                  type="button"
                                  disabled={!canManageAdoption}
                                  onClick={() => void handleUnlinkByGuidelineId(g.id)}
                                  className="inline-flex items-center gap-1 rounded border border-orange-200 px-2 py-1 text-[10px] text-orange-600 hover:bg-orange-50 disabled:cursor-not-allowed disabled:opacity-40 dark:border-orange-800 dark:text-orange-300 dark:hover:bg-orange-900/20"
                                  title="Unlink this guideline from the current board"
                                  data-testid={`guideline-unlink-board-${g.id}`}
                                >
                                  <Unlink size={11} />
                                  Unlink
                                </button>
                              </>
                            ) : (
                              <button
                                type="button"
                                disabled={
                                  !canOpenImpact
                                  || !candidate
                                  || !candidate.eligible
                                  || candidate.retired
                                  || impactOpeningId !== null
                                }
                                onClick={() => void openImpactPreview(g)}
                                className="inline-flex items-center gap-1 rounded border border-blue-200 px-2 py-1 text-[10px] font-medium text-blue-600 hover:bg-blue-50 disabled:cursor-not-allowed disabled:opacity-40 dark:border-blue-800 dark:text-blue-300 dark:hover:bg-blue-900/20"
                                title="Generate a persisted impact receipt before explicit adoption"
                                data-testid={`guideline-adopt-board-${g.id}`}
                              >
                                <ShieldCheck size={11} />
                                {impactOpeningId === g.id
                                  ? 'Loading latest…'
                                  : 'Preview & adopt'}
                              </button>
                            )}
                            <button
                              onClick={() => toggleDefault(g.id)}
                              disabled={
                                !canManageAdoption
                                ||
                                savingDefaults
                                || baseDefaultRefs === null
                                || !defaultInfo
                                || (!isDefault && (!candidate?.eligible || candidate.retired))
                              }
                              title="Stage this guideline as a global default for new boards"
                              data-testid={`guideline-set-default-${g.id}`}
                              className={`text-[10px] px-2 py-1 rounded border shrink-0 ${
                                isDefault
                                  ? 'bg-blue-500 text-white border-blue-500'
                                  : 'text-gray-500 border-gray-300 dark:border-gray-600'
                                } disabled:cursor-not-allowed disabled:opacity-40`}
                            >
                              {isDefault ? 'Default' : 'Set default'}
                            </button>
                            {defaultUpdateAvailable && (
                              <button
                                type="button"
                                disabled={savingDefaults || !canManageAdoption}
                                onClick={() => stageLatestDefaultRevision(g.id)}
                                data-testid={`guideline-default-use-latest-${g.id}`}
                                className="rounded border border-amber-300 px-2 py-1 text-[10px] font-medium text-amber-700 hover:bg-amber-50 disabled:opacity-40 dark:border-amber-700 dark:text-amber-200 dark:hover:bg-amber-950/20"
                              >
                                Use latest
                              </button>
                            )}
                            {canReadRevisions && (
                            <button
                              type="button"
                              onClick={() => openRevisionEditor(g, boardEntry)}
                              className="p-1.5 text-gray-400 hover:text-blue-500 hover:bg-blue-50 dark:hover:bg-blue-900/20 rounded"
                              title="Open immutable revision editor"
                              aria-label={`Open revision editor for ${g.title}`}
                            >
                              <Edit3 size={14} />
                            </button>
                            )}
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
              {!globalLoading && globalHasMore && !globalSearch && (
                <button
                  type="button"
                  disabled={globalLoadingMore}
                  onClick={() => void fetchGlobals(globals.length)}
                  data-testid="guidelines-load-more"
                  className="w-full rounded-md border border-gray-300 px-3 py-2 text-xs font-semibold text-gray-600 hover:bg-gray-50 disabled:opacity-40 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800"
                >
                  {globalLoadingMore ? 'Loading…' : 'Load more guidelines'}
                </button>
              )}
            </div>
          )}

          {/* ==================== WAIVERS TAB ==================== */}
          {activeTab === 'waivers' && canReadWaivers && (
            <PolicyWaiverPanel boardId={boardId} />
          )}
          </main>
        </div>
      </div>
      {revisionEditor && (
        <GuidelineRevisionEditor
          boardId={boardId}
          guideline={revisionEditor.guideline}
          adoptedRevision={revisionEditor.adoptedRevision}
          successorOptions={successorOptions}
          onClose={() => setRevisionEditor(null)}
          onChanged={refreshPolicyUi}
        />
      )}
      {impactDialog && (
        <GuidelineImpactDialog
          boardId={boardId}
          guidelineId={impactDialog.guidelineId}
          guidelineTitle={impactDialog.guidelineTitle}
          targetRevisionId={impactDialog.targetRevisionId}
          targetSemanticVersion={impactDialog.targetSemanticVersion}
          adoptedBinding={impactDialog.adoptedBinding}
          initialPriority={impactDialog.initialPriority}
          initialEnforcement={impactDialog.initialEnforcement}
          onClose={() => setImpactDialog(null)}
          onAdopted={handleAdopted}
        />
      )}
    </div>
  );
}

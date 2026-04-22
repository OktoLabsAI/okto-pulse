/**
 * SpecsPanel - List of specs for the current board
 */

import { useEffect, useState } from 'react';
import {
  Plus,
  FileText,
  Clock,
  CheckCircle2,
  Settings,
  Ban,
  ChevronRight,
  Archive,
  ArchiveRestore,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import type { SpecSummary, SpecStatus } from '@/types';
import { SPEC_STATUS_LABELS } from '@/types';
import { CreateSpecModal } from './CreateSpecModal';
import { SpecModal } from './SpecModal';

interface SpecsPanelProps {
  boardId: string;
}

const STATUS_ICON: Record<SpecStatus, React.ReactNode> = {
  draft: <FileText size={14} />,
  review: <Clock size={14} />,
  approved: <CheckCircle2 size={14} />,
  validated: <CheckCircle2 size={14} />,
  in_progress: <Settings size={14} />,
  done: <CheckCircle2 size={14} />,
  cancelled: <Ban size={14} />,
};

const STATUS_COLORS: Record<SpecStatus, string> = {
  draft: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
  review: 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/40 dark:text-yellow-300',
  approved: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  validated: 'bg-purple-100 text-purple-700 dark:bg-purple-900/40 dark:text-purple-300',
  in_progress: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  done: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
  cancelled: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
};

export function SpecsPanel({ boardId }: SpecsPanelProps) {
  const api = useDashboardApi();
  const [specs, setSpecs] = useState<SpecSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [createOpen, setCreateOpen] = useState(false);
  const [selectedSpecId, setSelectedSpecId] = useState<string | null>(null);
  const [filterStatus, setFilterStatus] = useState<string>('');
  const [showArchived, setShowArchived] = useState(false);

  useEffect(() => {
    loadSpecs();
  }, [boardId, filterStatus, showArchived]);

  const loadSpecs = async () => {
    setLoading(true);
    try {
      const data = await api.listSpecs(boardId, filterStatus || undefined, showArchived);
      setSpecs(data);
    } catch {
      toast.error('Failed to load specs');
    } finally {
      setLoading(false);
    }
  };

  const statusFilters = [
    { value: '', label: 'All' },
    { value: 'draft', label: 'Draft' },
    { value: 'review', label: 'Review' },
    { value: 'approved', label: 'Approved' },
    { value: 'validated', label: 'Validated' },
    { value: 'in_progress', label: 'In Progress' },
    { value: 'done', label: 'Done' },
  ];


  return (
    <div className="h-full flex flex-col">
      {/* Toolbar */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Specifications</h2>
          <span className="text-sm text-gray-400">({specs.length})</span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setCreateOpen(true)}
            className="btn btn-primary flex items-center gap-1 text-sm"
          >
            <Plus size={16} />
            New Spec
          </button>
        </div>
      </div>

      {/* Status filter pills */}
      <div className="flex gap-1.5 mb-4 flex-wrap">
        {statusFilters.map((f) => (
          <button
            key={f.value}
            onClick={() => setFilterStatus(f.value)}
            className={`text-xs px-2.5 py-1 rounded-full transition-colors ${
              filterStatus === f.value
                ? 'bg-accent-500 text-white shadow-sm'
                : 'bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600'
            }`}
          >
            {f.label}
          </button>
        ))}
        <button
          onClick={() => setShowArchived(!showArchived)}
          className={`text-xs px-2.5 py-1 rounded-full transition-colors ml-2 ${
            showArchived
              ? 'bg-amber-500 text-white'
              : 'bg-gray-100 text-gray-500 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-400'
          }`}
        >
          {showArchived ? 'Hide archived' : 'Show archived'}
        </button>
      </div>

      {/* Spec list */}
      <div className="flex-1 overflow-y-auto space-y-2">
        {loading ? (
          <div className="text-center text-gray-500 dark:text-gray-400 py-8">Loading specs...</div>
        ) : specs.length === 0 ? (
          <div className="text-center py-12">
            <FileText size={40} className="mx-auto text-gray-300 dark:text-gray-600 mb-3" />
            <p className="text-gray-500 dark:text-gray-400 mb-2">
              {filterStatus ? 'No specs with this status' : 'No specs yet'}
            </p>
            <p className="text-sm text-gray-400 dark:text-gray-500 mb-4">
              Specs define requirements before creating tasks
            </p>
            <button
              onClick={() => setCreateOpen(true)}
              className="btn btn-primary text-sm"
            >
              Create your first spec
            </button>
          </div>
        ) : (
          specs.map((spec) => (
            <div
              key={spec.id}
              onClick={() => setSelectedSpecId(spec.id)}
              className={`group bg-white dark:bg-surface-800/80 border border-surface-200/80 dark:border-surface-700/40 rounded-xl p-4 cursor-pointer
                hover:border-accent-300 dark:hover:border-accent-600/40 hover:shadow-card-hover dark:hover:shadow-card-dark-hover transition-all duration-200 ${spec.archived ? 'opacity-50' : ''}`}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[spec.status]}`}>
                      {STATUS_ICON[spec.status]}
                      {SPEC_STATUS_LABELS[spec.status]}
                    </span>
                    <span className="text-xs text-gray-400">v{spec.version}</span>
                  </div>
                  <h3 className="font-medium text-gray-900 dark:text-white text-sm truncate">
                    {spec.title}
                  </h3>
                  {spec.description && (
                    <p className="text-xs text-gray-500 dark:text-gray-400 mt-1 line-clamp-2">
                      {spec.description}
                    </p>
                  )}
                  {/* Labels */}
                  {spec.labels && spec.labels.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-2">
                      {spec.labels.map((label, i) => (
                        <span
                          key={i}
                          className="text-xs px-1.5 py-0.5 rounded bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300"
                        >
                          {label}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
                <div className="flex items-center gap-1 shrink-0">
                  {spec.archived && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-200 text-gray-500 dark:bg-gray-700 dark:text-gray-400 font-medium">archived</span>
                  )}
                  <button
                    onClick={async (e) => {
                      e.stopPropagation();
                      try {
                        if (spec.archived) {
                          await api.restoreTree(boardId, 'spec', spec.id);
                          toast.success('Tree restored');
                        } else {
                          await api.archiveTree(boardId, 'spec', spec.id);
                          toast.success('Tree archived');
                        }
                        loadSpecs();
                      } catch { toast.error('Failed'); }
                    }}
                    className="p-1 text-gray-400 hover:text-blue-500 opacity-0 group-hover:opacity-100 transition-opacity"
                    title={spec.archived ? 'Restore tree' : 'Archive tree'}
                  >
                    {spec.archived ? <ArchiveRestore size={14} /> : <Archive size={14} />}
                  </button>
                  <ChevronRight
                    size={16}
                    className="text-gray-300 dark:text-gray-600 group-hover:text-blue-500 mt-1 transition-colors"
                  />
                </div>
              </div>
            </div>
          ))
        )}
      </div>

      {/* Modals */}
      {createOpen && (
        <CreateSpecModal
          boardId={boardId}
          onClose={() => setCreateOpen(false)}
          onCreated={() => loadSpecs()}
        />
      )}

      {selectedSpecId && (
        <SpecModal
          specId={selectedSpecId}
          boardId={boardId}
          onClose={() => setSelectedSpecId(null)}
          onChanged={loadSpecs}
        />
      )}
    </div>
  );
}

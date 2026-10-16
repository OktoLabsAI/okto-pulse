/** Board-scoped right to erasure, independent of technical KG configuration. */
import { useState } from 'react';
import toast from 'react-hot-toast';
import * as kgApi from '@/services/kg-api';
import { usePermissions } from '@/hooks/usePermissions';

export function PrivacyView({ boardId }: { boardId: string }) {
  const perms = usePermissions(boardId);
  const canErase = !perms.isLoading && !perms.error && !perms.ownerReviewRequired
    && perms.has('kg.operations.board.erase');
  const [deleting, setDeleting] = useState(false);

  return (
    <div className="p-6 max-w-2xl">
      <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
        Knowledge Graph Privacy
      </h2>
      <p className="mt-3 text-sm text-gray-600 dark:text-gray-400">
        Permanently delete knowledge graph data for this board.
      </p>
      {canErase && (
        <button
          disabled={deleting}
          onClick={async () => {
            if (!canErase || deleting) return;
            if (!confirm('This will permanently delete all KG data for this board. Continue?')) return;
            setDeleting(true);
            try {
              await kgApi.deleteKG(boardId);
              toast.success('Knowledge graph data deleted');
            } catch (err: unknown) {
              toast.error(err instanceof Error ? err.message : 'Failed to delete KG data');
            } finally {
              setDeleting(false);
            }
          }}
          className="mt-6 px-4 py-2 text-sm border border-red-300 dark:border-red-700 text-red-600 dark:text-red-400 rounded-lg hover:bg-red-50 dark:hover:bg-red-900/20 disabled:opacity-50"
        >
          {deleting ? 'Deleting…' : 'Delete KG Data'}
        </button>
      )}
    </div>
  );
}

import { Download } from 'lucide-react';
import { useRef, useState } from 'react';

import type { EntityExportType } from '@/types/entity-export';

import { EntityExportDialog } from './EntityExportDialog';

interface EntityExportButtonProps {
  boardId: string;
  entityType: EntityExportType;
  entityId: string;
  entityTitle: string;
  disabled?: boolean;
  iconSize?: number;
  className?: string;
}

export function EntityExportButton({
  boardId,
  entityType,
  entityId,
  entityTitle,
  disabled = false,
  iconSize = 16,
  className = 'p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors disabled:opacity-30',
}: EntityExportButtonProps) {
  const [open, setOpen] = useState(false);
  const openerRef = useRef<HTMLButtonElement>(null);
  return (
    <>
      <button
        ref={openerRef}
        type="button"
        disabled={disabled}
        onClick={() => setOpen(true)}
        className={className}
        title="Export report"
        aria-haspopup="dialog"
      >
        <Download size={iconSize} />
      </button>
      {open && (
        <EntityExportDialog
          boardId={boardId}
          entityType={entityType}
          entityId={entityId}
          entityTitle={entityTitle}
          opener={openerRef.current}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}

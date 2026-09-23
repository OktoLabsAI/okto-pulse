/** Empty graph: product guidance without a maintenance workflow. */
import { useState } from 'react';
import { KGHelpModal } from './KGHelpModal';

interface Props {
  boardId: string;
  onRefresh?: () => void;
}

export function EmptyState(_props: Props) {
  const [showHelp, setShowHelp] = useState(false);
  return (
    <div className="flex flex-col items-center justify-center h-full text-center p-8" data-tour-id="kg.discovery.search" role="status">
      <div className="text-6xl mb-4">🕸️</div>
      <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100 mb-2">Knowledge Graph is empty</h2>
      <p className="text-gray-500 dark:text-gray-400 mb-6 max-w-md">
        This board has no consolidated knowledge yet. Work on Specs and Cards to build its context.
      </p>
      <button onClick={() => setShowHelp(true)} className="px-4 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-300 text-sm">
        Learn How It Works
      </button>
      {showHelp && <KGHelpModal onClose={() => setShowHelp(false)} />}
    </div>
  );
}

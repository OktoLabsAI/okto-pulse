/**
 * EmptyState — shown when a board has no KG data yet.
 * Hero + CTA to enable historical consolidation.
 */

interface Props {
  boardId: string;
}

export function EmptyState({ boardId }: Props) {
  return (
    <div className="flex flex-col items-center justify-center h-full text-center p-8" role="status">
      <div className="text-6xl mb-4">🕸️</div>
      <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100 mb-2">
        Knowledge Graph ainda vazio
      </h2>
      <p className="text-gray-500 dark:text-gray-400 mb-6 max-w-md">
        Este board ainda nao tem dados consolidados no knowledge graph.
        Ative a consolidacao historica para processar specs e sprints existentes,
        ou aguarde novas consolidacoes via o code agent.
      </p>
      <div className="flex gap-3">
        <button className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium">
          Enable Historical Consolidation
        </button>
        <button className="px-4 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-300 text-sm">
          Learn How It Works
        </button>
      </div>
    </div>
  );
}

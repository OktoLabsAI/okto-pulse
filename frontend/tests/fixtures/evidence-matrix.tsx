import { createRoot } from 'react-dom/client';
import { ApiProvider } from '../../src/contexts/ApiContext';
import { adapterReady } from '../../src/adapters';
import { EvidenceMatrixPanel } from '../../src/components/code-traceability/EvidenceMatrixPanel';
import '../../src/index.css';

await adapterReady;
createRoot(document.getElementById('root')!).render(
  <ApiProvider>
    <main className="m-4 max-w-4xl rounded-xl bg-white p-5 dark:bg-gray-800">
      <EvidenceMatrixPanel boardId="fixture" subjectId="spec-1" subjectVersion={1}
        obligationTitles={{ 'tr-history': 'Preserve native snapshot boundaries' }} />
    </main>
  </ApiProvider>,
);

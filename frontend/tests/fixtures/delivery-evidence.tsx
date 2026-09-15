import { createRoot } from 'react-dom/client';
import { ApiProvider } from '../../src/contexts/ApiContext';
import { adapterReady } from '../../src/adapters';
import { DeliveryEvidencePanel } from '../../src/components/code-traceability/DeliveryEvidencePanel';
import '../../src/index.css';

await adapterReady;
createRoot(document.getElementById('root')!).render(<ApiProvider><main className="m-4 max-w-5xl rounded-xl bg-white p-5 text-slate-900 dark:bg-slate-800 dark:text-slate-100"><DeliveryEvidencePanel boardId="fixture" specId="spec-1" canRecord canTest canWaive /></main></ApiProvider>);

import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ArchitectureDiagramEditor } from '../../src/components/architecture/ArchitectureDiagramEditor';
import type { ArchitectureDiagram } from '../../src/types';
import '../../src/index.css';

const initial: ArchitectureDiagram = {
  id: 'isolated-margin-test', title: 'Negative-coordinate architecture',
  diagram_type: 'container', format: 'excalidraw_json', description: null, order_index: 0,
  adapter_payload: { type: 'excalidraw', version: 2, appState: {}, files: {}, elements: [
    { id: 'adapter', type: 'rectangle', x: -360, y: -240, width: 240, height: 100, text: 'Grafx adapter', displayType: 'Service' },
    { id: 'local', type: 'rectangle', x: -540, y: 24, width: 240, height: 100, text: 'Current project store', displayType: 'Database' },
    { id: 'global', type: 'rectangle', x: 96, y: 24, width: 240, height: 100, text: 'User global store', displayType: 'Database' },
    { id: 'edge', type: 'arrow', sourceElementId: 'adapter', targetElementId: 'local', text: 'Local persistence' },
  ] },
};
function Fixture() {
  const [diagram, setDiagram] = useState(initial);
  const [readOnly, setReadOnly] = useState(false);
  const [focusSignal, setFocusSignal] = useState(0);
  return <main className="h-screen bg-gray-950 text-gray-100 p-6">
    <h1>Pulse architecture — isolated instance, synthetic data only</h1>
    <div className="flex gap-6 p-2">
      <button onClick={() => setReadOnly(!readOnly)}>Toggle read only</button>
      <button onClick={() => setFocusSignal(focusSignal + 1)}>Focus left node</button>
    </div>
    <div style={{ height: 'min(700px, 80vh)' }} className="border border-gray-700 rounded-lg overflow-hidden">
      <ArchitectureDiagramEditor diagram={diagram} onChange={setDiagram} readOnly={readOnly}
        focusElementId={focusSignal ? 'local' : null} focusSignal={focusSignal} />
    </div>
    <output data-testid="stored-diagram" className="hidden">{JSON.stringify(diagram)}</output>
  </main>;
}
createRoot(document.getElementById('root')!).render(<Fixture />);

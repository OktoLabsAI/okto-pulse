import React from "react";
import { createRoot } from "react-dom/client";
import { ApiProvider } from "../../src/contexts/ApiContext";
import {
  ModalStackProvider,
  useModalStack,
} from "../../src/contexts/ModalStackContext";
import { CognitiveActionCenterView } from "../../src/components/knowledge/CognitiveActionCenterView";
import { adapterReady } from "../../src/adapters";
import "../../src/index.css";
function Fixture() {
  const { stack } = useModalStack();
  const [destination, setDestination] = React.useState("");
  return (
    <main className="h-screen">
      <CognitiveActionCenterView
        boardId="fixture"
        boardName="Example project"
        onClose={() => setDestination("Board")}
        onOpenHealth={() => setDestination("KG Health")}
      />
      {(stack.length > 0 || destination) && (
        <aside
          role="status"
          className="fixed bottom-2 right-2 z-[80] bg-violet-700 text-white p-3"
        >
          Opened: {destination || `${stack[0].type}:${stack[0].id}`}
        </aside>
      )}
    </main>
  );
}
await adapterReady;
createRoot(document.getElementById("root")!).render(
  <ApiProvider>
    <ModalStackProvider>
      <Fixture />
    </ModalStackProvider>
  </ApiProvider>,
);

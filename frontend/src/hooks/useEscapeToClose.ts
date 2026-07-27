import { useEffect, useRef } from 'react';

interface EscapeLayer {
  id: symbol;
  activationOrder: number;
  priority: () => number;
  canClose: () => boolean;
  close: () => void;
}

export interface EscapeToCloseOptions {
  /** Register this layer only while its modal is visible. */
  enabled?: boolean;
  /**
   * Keep the layer registered but consume Escape without closing it.
   * This prevents an Escape pressed during a guarded submit/save from
   * falling through to a modal underneath.
   */
  canClose?: boolean;
  /**
   * Layers that can be mounted together with their parent should use a
   * higher priority. Within the same priority, the most recently activated
   * layer wins.
   */
  priority?: number;
}

const layers: EscapeLayer[] = [];
let listening = false;
let activationSequence = 0;

function handleEscape(event: KeyboardEvent) {
  if (event.key !== 'Escape' || event.defaultPrevented) return;

  const top = layers.reduce<EscapeLayer | undefined>(
    (current, layer) => {
      if (!current) return layer;
      const priorityDelta = layer.priority() - current.priority();
      if (priorityDelta !== 0) return priorityDelta > 0 ? layer : current;
      return layer.activationOrder > current.activationOrder ? layer : current;
    },
    undefined,
  );
  if (!top) return;

  event.preventDefault();
  event.stopPropagation();

  if (top.canClose()) top.close();
}

function startListening() {
  if (listening || typeof document === 'undefined') return;
  document.addEventListener('keydown', handleEscape);
  listening = true;
}

function stopListeningWhenIdle() {
  if (!listening || layers.length > 0 || typeof document === 'undefined') return;
  document.removeEventListener('keydown', handleEscape);
  listening = false;
}

/**
 * Close the uppermost registered modal with Escape.
 *
 * Registrations form a stack, so nested dialogs close one layer at a time.
 * Callback and guard refs update without re-registering the layer; parent
 * re-renders therefore cannot jump ahead of an already-open child modal.
 */
export function useEscapeToClose(
  onClose: () => void,
  { enabled = true, canClose = true, priority = 0 }: EscapeToCloseOptions = {},
) {
  const closeRef = useRef(onClose);
  const canCloseRef = useRef(canClose);
  const priorityRef = useRef(priority);
  const idRef = useRef(Symbol('modal-escape-layer'));

  closeRef.current = onClose;
  canCloseRef.current = canClose;
  priorityRef.current = priority;

  useEffect(() => {
    if (!enabled || typeof document === 'undefined') return;

    const layer: EscapeLayer = {
      id: idRef.current,
      activationOrder: ++activationSequence,
      priority: () => priorityRef.current,
      canClose: () => canCloseRef.current,
      close: () => closeRef.current(),
    };
    layers.push(layer);
    startListening();

    return () => {
      const index = layers.findIndex((candidate) => candidate.id === layer.id);
      if (index >= 0) layers.splice(index, 1);
      stopListeningWhenIdle();
    };
  }, [enabled]);
}

import { useEffect, useRef, useState } from 'react';
import { useOptionalModalStack } from '@/contexts/ModalStackContext';
import { useDashboardApi } from '@/services/api';
import { readinessSpecLocation, type ReadinessNavigator, type ReadinessSpecLocation } from './readinessNavigation';

export function useNativeReadinessNavigation(boardId: string, specId: string,
  openSpec?: (location: ReadinessSpecLocation) => void) {
  const stack = useOptionalModalStack();
  const api = useDashboardApi();
  const [error, setError] = useState<string>();
  const generation = useRef(0);
  useEffect(() => {
    const invalidate = () => { generation.current++; };
    invalidate(); setError(undefined);
    return invalidate;
  }, [boardId, specId]);
  const navigator: ReadinessNavigator = {
    supports: destination => destination.type === 'card' ? !!stack
      : !!readinessSpecLocation(destination, specId) && (!!openSpec || !!stack),
    open: async destination => {
      const request = ++generation.current;
      setError(undefined);
      if (destination.type === 'card' && stack) {
        try {
          // Card reads are authorized by the native API; also retain the diagnostic's exact scope.
          const card = await api.getCard(destination.id);
          if (generation.current !== request) return;
          if (card.id !== destination.id || card.board_id !== boardId || card.spec_id !== specId)
            throw new Error('The referenced Card no longer belongs to this Board and Spec. Refresh the diagnosis.');
          stack.push({ type: 'card', id: card.id, boardId });
        } catch (failure) {
          if (generation.current === request) setError(failure instanceof Error ? failure.message : 'The referenced Card is unavailable.');
        }
        return;
      }
      const location = readinessSpecLocation(destination, specId);
      if (!location) return;
      try {
        const spec = await api.getSpec(specId);
        if (generation.current !== request) return;
        if (spec.id !== specId || spec.board_id !== boardId)
          throw new Error('The referenced Spec no longer belongs to this Board. Refresh the diagnosis.');
        if (openSpec) openSpec(location);
        else stack?.push({ type: 'spec', id: specId, boardId, initialTab: location.tab, focusReadinessRef: location.ref });
      } catch (failure) {
        if (generation.current === request) setError(failure instanceof Error ? failure.message : 'The referenced Spec is unavailable.');
      }
    },
  };
  return { navigator, error };
}

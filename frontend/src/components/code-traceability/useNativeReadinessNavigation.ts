import { useEffect, useRef, useState } from 'react';
import { useOptionalModalStack } from '@/contexts/ModalStackContext';
import { useDashboardApi } from '@/services/api';
import { readinessSpecLocation, type ReadinessCardLocation, type ReadinessDestination, type ReadinessNavigator, type ReadinessSpecLocation } from './readinessNavigation';

export function useNativeReadinessNavigation(boardId: string, specId: string,
  openSpec?: (location: ReadinessSpecLocation) => void, openCard?: (id: string, target?: ReadinessCardLocation) => void) {
  const stack = useOptionalModalStack();
  const api = useDashboardApi();
  const [error, setError] = useState<string>();
  const generation = useRef(0);
  useEffect(() => {
    const invalidate = () => { generation.current++; };
    invalidate(); setError(undefined);
    return invalidate;
  }, [boardId, specId]);
  const normalize = (destination: ReadinessDestination): ReadinessDestination => destination.type === 'obligation'
    && destination.id.startsWith('card:') && destination.id.length > 5
    ? { type: 'card', id: destination.id.slice(5) } : destination;
  const showCard = (id: string, target?: ReadinessCardLocation) => {
    if (openCard) openCard(id, target);
    else stack?.push({ type: 'card', id, boardId, ...(target ? { target } : {}) });
  };
  const navigator: ReadinessNavigator = {
    supports: input => {
      const destination = normalize(input);
      return ['card', 'target', 'execution', 'record'].includes(destination.type) ? !!stack || !!openCard
        : !!readinessSpecLocation(destination, specId) && (!!openSpec || !!stack);
    },
    open: async input => {
      const destination = normalize(input);
      const request = ++generation.current;
      setError(undefined);
      if (['card', 'target', 'execution', 'record'].includes(destination.type) && (stack || openCard)) {
        try {
          let cardId = destination.type === 'card' ? destination.id : destination.cardId;
          if (!cardId) {
            const data = await api.getDeliveryEvidence(boardId, specId);
            if (generation.current !== request) return;
            if (data.board_id !== boardId || data.spec_id !== specId) throw new Error('Evidence scope changed. Refresh the diagnosis.');
            const owners = new Set<string>();
            if (destination.type === 'record') {
              const record = data.records.find(row => row.id === destination.id);
              if (record?.payload.card_id) owners.add(record.payload.card_id);
              for (const row of [...data.implementations, ...(data.tests ?? [])]) if (row.id === destination.id) owners.add(row.card_id);
            } else {
              for (const proof of data.implementations) if (proof.executions.some(execution => destination.type === 'target'
                ? execution.target_id === destination.id : execution.execution_id === destination.id)) owners.add(proof.card_id);
            }
            if (owners.size !== 1) throw new Error('The referenced evidence owner is unavailable or ambiguous in the current authorized scope. Refresh the diagnosis.');
            cardId = [...owners][0];
          }
          // Card reads are authorized by the native API; also retain the diagnostic's exact scope.
          const card = await api.getCard(cardId);
          if (generation.current !== request) return;
          if (card.id !== cardId || card.board_id !== boardId || card.spec_id !== specId)
            throw new Error('The referenced Card no longer belongs to this Board and Spec. Refresh the diagnosis.');
          if (destination.type === 'card') showCard(card.id);
          else if (destination.type === 'record') showCard(card.id,
            { tab: 'delivery', focusDiagnosis: false, focusReference: `record:${destination.id}`, recordId: destination.id });
          else {
            if (card.card_type !== 'normal') throw new Error('Native implementation Targets are unavailable for this Card type.');
            const projection = await api.getCodeTraceabilityProjection(boardId, 'card', card.id, card.subject_version ?? 1);
            if (generation.current !== request) return;
            const found = destination.type === 'target' ? projection.targets.some(target => target.id === destination.id)
              : projection.executions?.some(execution => execution.id === destination.id);
            if (projection.subject_id !== card.id || projection.subject_type !== 'card' || !found)
              throw new Error('The referenced Target or execution is unavailable in this Card scope.');
            showCard(card.id, { tab: 'implementation-targets', focusDiagnosis: false,
              focusReference: `${destination.type}:${destination.id}` });
          }
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

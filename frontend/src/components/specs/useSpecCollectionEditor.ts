import { useCallback, useEffect, useRef, useState } from 'react';
import type { Spec } from '@/types';
import { isSpecEntityVersionConflict } from './specEntityErrors';

/** Local draft guard for the six native structured-collection editors. */
export function useSpecCollectionEditor(spec: Spec, readCurrent: boolean) {
  const [base, setBase] = useState<{ version: number; edition: number } | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [partial, setPartial] = useState(false);
  const pending = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const changed = !!base && (base.version !== spec.version || base.edition !== spec.edition);
  const unavailable = !readCurrent || !Number.isInteger(spec.version);
  const blocked = saving || unavailable || changed || conflict || partial;
  const begin = useCallback(() => {
    if (pending.current || unavailable) return false;
    setBase({ version: spec.version, edition: spec.edition });
    setError(''); setConflict(false); setPartial(false);
    return true;
  }, [spec.version, spec.edition, unavailable]);
  const review = () => {
    if (pending.current || unavailable || partial) return;
    // A server conflict cannot be acknowledged against the same stale read.
    if (conflict && !changed) return;
    setBase({ version: spec.version, edition: spec.edition });
    setConflict(false); setError('');
  };
  const save = async (write: (version: number) => void | Promise<void>, direct = false) => {
    if (pending.current || unavailable || (!direct && (blocked || !base))) return false;
    pending.current = true; setSaving(true); setError('');
    try {
      await write(direct ? spec.version : base!.version);
      if (!mounted.current) return false;
      if (!direct) setBase(null);
      return true;
    } catch (cause) {
      if (!mounted.current) return false;
      setError(cause instanceof Error ? cause.message : 'Could not save. Your draft is preserved.');
      setConflict(isSpecEntityVersionConflict(cause));
      setPartial(!!cause && typeof cause === 'object' && 'code' in cause && cause.code === 'partial_collection_write');
      return false;
    } finally {
      pending.current = false;
      if (mounted.current) setSaving(false);
    }
  };
  const cancel = () => { setBase(null); setError(''); setConflict(false); setPartial(false); };
  return { begin, review, save, cancel, saving, blocked, changed, unavailable, conflict, partial, error };
}

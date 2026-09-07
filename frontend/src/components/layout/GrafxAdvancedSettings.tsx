import { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Info } from 'lucide-react';
import type { GrafxSettingDescriptor } from '@/services/runtime-settings-api';

export function SettingHelp({ label, text }: { label: string; text: string }) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const [position, setPosition] = useState<{ left: number; top?: number; bottom?: number }>({ left: 8, top: 8 });
  const show = () => {
    const box = buttonRef.current?.getBoundingClientRect();
    if (!box) return;
    const left = Math.max(8, Math.min(box.left, window.innerWidth - 368));
    setPosition(box.top > window.innerHeight / 2
      ? { left, bottom: window.innerHeight - box.top + 8 }
      : { left, top: box.bottom + 8 });
    setOpen(true);
  };
  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    window.addEventListener('resize', close);
    document.addEventListener('scroll', close, true);
    return () => {
      window.removeEventListener('resize', close);
      document.removeEventListener('scroll', close, true);
    };
  }, [open]);
  return (
    <span className="inline-block ml-1 align-middle">
      <button ref={buttonRef} type="button" aria-label={`About ${label}`} aria-describedby={open ? id : undefined}
        onMouseEnter={show} onMouseLeave={() => setOpen(false)}
        onFocus={show} onBlur={() => setOpen(false)} onClick={show}
        onKeyDown={(e) => { if (e.key === 'Escape') { e.stopPropagation(); setOpen(false); } }}
        className="text-blue-500 rounded focus-visible:outline focus-visible:outline-2">
        <Info size={13} />
      </button>
      {open && createPortal(<span id={id} role="tooltip" style={position}
        className="fixed z-[100] w-[360px] max-w-[calc(100vw-16px)] p-2 rounded shadow-lg border border-blue-300 bg-blue-50 text-xs font-normal text-blue-950 dark:bg-gray-800 dark:text-blue-100">{text}</span>, document.body)}
    </span>
  );
}

type Options = Record<string, number | string | null>;

export function GrafxAdvancedSettings({ catalog, value, onChange }: {
  catalog: GrafxSettingDescriptor[]; value: Options; onChange: (value: Options) => void;
}) {
  const advanced = catalog.filter((item) => !item.alias);
  return (
    <details data-testid="grafx-advanced-settings" className="border rounded-lg p-3 dark:border-gray-700">
      <summary className="cursor-pointer text-xs font-semibold">Advanced Grafx settings ({advanced.length})</summary>
      <p className="text-xs text-gray-500 my-3">Changes require a process restart, not a graph rebuild. Empty optional limits mean no configured cap. Low budgets can reject legitimate Pulse operations; they never silently truncate results. Managed options are listed for completeness.</p>
      {advanced.length === 0 && <p className="text-xs">Restart/update the backend to load the native configuration catalog.</p>}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        {advanced.map((item) => {
          const current = Object.prototype.hasOwnProperty.call(value, item.name) ? value[item.name] : item.default;
          const id = `grafx-option-${item.name}`;
          return <div key={item.name} className="min-w-0">
            <div className="text-xs font-medium break-words">
              <label htmlFor={id}>{item.name.replace(/_/g, ' ')}</label>
              <SettingHelp label={item.name} text={item.description} />
            </div>
            <div className="text-[10px] text-gray-500 mb-1">{item.name}</div>
            {!item.editable ? <div data-testid={id} className="text-xs text-gray-500">
              Managed by Pulse <span className="block text-[10px]">{item.description}</span>
            </div> : item.choices ? <select id={id} data-testid={id} aria-label={item.name} value={String(current ?? '')}
              onChange={(e) => onChange({ ...value, [item.name]: e.target.value })}
              className="w-full rounded border p-1.5 text-xs bg-white dark:bg-gray-800 dark:border-gray-600">
              {item.choices.map((choice) => <option key={choice} value={choice}>{choice}</option>)}
            </select> : <input id={id} data-testid={id} aria-label={item.name} type="number"
              min={item.name === 'vector_exact_scan_threshold' ? 0 : undefined}
              step={item.name.endsWith('_seconds') ? 'any' : 1}
              value={typeof current === 'number' && !Number.isFinite(current) ? '' : String(current ?? '')}
              placeholder={item.nullable ? 'No configured cap' : undefined}
              onChange={(e) => onChange({ ...value, [item.name]: e.target.value === '' ? (item.nullable ? null : NaN) : Number(e.target.value) })}
              className="w-full rounded border p-1.5 text-xs bg-white dark:bg-gray-800 dark:border-gray-600" />}
          </div>;
        })}
      </div>
    </details>
  );
}

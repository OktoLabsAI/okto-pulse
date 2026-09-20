import { useEffect, useState } from 'react';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import { useHistoricalArchivesApi, type ArchiveItem, type ArchiveList, type ArchivePage, type ArchiveSection } from '@/services/historical-archives-api';

const sectionLabels: Record<ArchiveSection, string> = {
  content: 'Content', qa: 'Questions & answers', evaluations: 'Evaluations', history: 'History',
};
const buttonClass = 'rounded border border-surface-300 dark:border-surface-600 px-3 py-2 text-sm hover:bg-surface-100 dark:hover:bg-surface-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent-600 disabled:opacity-50';

function errorMessage(error: unknown): string {
  if (error instanceof AuthenticatedFetchError) {
    if ([401, 403, 404].includes(error.status)) return 'This archive is unavailable or you no longer have access.';
    if (error.status === 413) return 'This archive exceeds the supported reading limit.';
    if (error.status === 503) return 'The archive could not be verified. Try again later.';
  }
  return 'Unable to load the archive. Please try again.';
}

function ArchiveRecords({ boardId, item, section, offset, onNext }: {
  boardId: string; item: ArchiveItem; section: ArchiveSection; offset: number; onNext: (offset: number) => void;
}) {
  const api = useHistoricalArchivesApi();
  const [page, setPage] = useState<ArchivePage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setPage(null);
    setError(null);
    api.read(boardId, item, section, offset, controller.signal).then(result => {
      if (!controller.signal.aborted) setPage(result);
    }).catch(error => {
      if (!controller.signal.aborted) setError(errorMessage(error));
    });
    return () => controller.abort();
  }, [api, boardId, item, section, offset, retry]);
  if (error) return <div role="alert"><p>{error}</p><button className={buttonClass} onClick={() => setRetry(value => value + 1)}>Retry section</button></div>;
  if (!page) return <p role="status">Loading archived section…</p>;
  return <div className="space-y-4">
    {page.records.length === 0 && <p>No records in this section.</p>}
    {page.records.map((record, index) => <article key={index} aria-label={`Archived record ${offset + index + 1}`} className="rounded-lg border border-surface-200 dark:border-surface-700 p-4">
      <dl className="space-y-3">
        {Object.entries(record).map(([field, value]) => <div key={field}>
          <dt className="text-sm font-semibold capitalize">{field.replace(/_/g, ' ')}</dt>
          <dd className="whitespace-pre-wrap break-words text-sm [overflow-wrap:anywhere]">
            {value === null ? '—' : typeof value === 'string' ? value : JSON.stringify(value, null, 2)}
          </dd>
        </div>)}
      </dl>
    </article>)}
    {page.next_offset !== null && <button className={buttonClass} onClick={() => onNext(page.next_offset!)}>Next section page</button>}
  </div>;
}

function ArchiveDetail({ boardId, item, onBack }: { boardId: string; item: ArchiveItem; onBack: () => void }) {
  const [section, setSection] = useState<ArchiveSection>('content');
  const [offset, setOffset] = useState(0);
  return <div className="space-y-4">
    <button className={buttonClass} onClick={onBack}>Back to archived origins</button>
    <h3 className="break-words font-semibold">Historical origin: {item.origin.kind} · {item.origin.id}</h3>
    <p className="text-sm text-surface-600 dark:text-surface-400">Original evidence is read-only. It does not approve current work. References below identify historical sources.</p>
    <div role="group" aria-label="Archived sections" className="flex flex-wrap gap-2">
      {item.sections.map(value => <button key={value} className={buttonClass} aria-pressed={section === value}
        onClick={() => { setSection(value); setOffset(0); }}>{sectionLabels[value]}</button>)}
    </div>
    {offset > 0 && <button className={buttonClass} onClick={() => setOffset(0)}>First section page</button>}
    <ArchiveRecords key={`${section}:${offset}`} boardId={boardId} item={item} section={section} offset={offset} onNext={setOffset} />
  </div>;
}

function ArchiveListView({ boardId }: { boardId: string }) {
  const api = useHistoricalArchivesApi();
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<ArchiveList | null>(null);
  const [selected, setSelected] = useState<ArchiveItem | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setPage(null);
    setError(null);
    api.list(boardId, offset, controller.signal).then(result => {
      if (!controller.signal.aborted) setPage(result);
    }).catch(error => {
      if (!controller.signal.aborted) setError(errorMessage(error));
    });
    return () => controller.abort();
  }, [api, boardId, offset, retry]);
  if (selected) return <ArchiveDetail boardId={boardId} item={selected} onBack={() => { setSelected(null); setPage(null); setRetry(value => value + 1); }} />;
  return <div className="space-y-4">
    <p className="text-sm text-surface-600 dark:text-surface-400">Browse historical origins you can currently access. Content is verified when you open a section.</p>
    {error ? <div role="alert"><p>{error}</p><button className={buttonClass} onClick={() => setRetry(value => value + 1)}>Retry archives</button></div>
      : !page ? <p role="status">Loading archived origins…</p>
        : page.items.length === 0 ? <p>No archived origins available.</p>
          : <ul className="space-y-2">{page.items.map(item => <li key={JSON.stringify(item.origin)}>
            <button className={`${buttonClass} w-full break-words text-left`} onClick={() => setSelected(item)}>
              {item.origin.kind} · {item.origin.id}
            </button>
          </li>)}</ul>}
    <div className="flex flex-wrap gap-2">
      {offset > 0 && <button className={buttonClass} onClick={() => { setPage(null); setOffset(0); }}>First origins page</button>}
      {page?.next_offset != null && <button className={buttonClass} onClick={() => { setPage(null); setOffset(page.next_offset!); }}>Next origins page</button>}
      <button className={buttonClass} onClick={() => { setPage(null); setOffset(0); setRetry(value => value + 1); }}>Refresh archives</button>
    </div>
  </div>;
}

export function HistoricalArchivesPanel({ boardId }: { boardId: string }) {
  // Remount even when used outside BoardStageContent: never retain another Board's records.
  return <section className="h-full overflow-auto p-4 text-surface-900 dark:text-surface-100" aria-label="Historical archives">
    <h2 className="mb-4 text-lg font-semibold">Historical archives</h2>
    <ArchiveListView key={boardId} boardId={boardId} />
  </section>;
}

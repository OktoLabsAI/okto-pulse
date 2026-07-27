export type PageToken = number | 'ellipsis-start' | 'ellipsis-end';

function nonNegativeInteger(value: number): number {
  return Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0;
}
/** Number of pages is driven exclusively by the filtered result count. */
export function paginationPageCount(totalFiltered: number, pageSize: number): number {
  const total = nonNegativeInteger(totalFiltered);
  const size = Number.isFinite(pageSize) ? Math.max(1, Math.floor(pageSize)) : 1;
  return total === 0 ? 0 : Math.ceil(total / size);
}

/** Compact page-number window with stable, uniquely-keyed ellipsis tokens. */
export function visiblePaginationPages(page: number, pageCount: number): PageToken[] {
  if (pageCount <= 0) return [];
  if (pageCount <= 7) return Array.from({ length: pageCount }, (_, index) => index + 1);

  const candidates = new Set([1, pageCount]);
  for (let candidate = page - 1; candidate <= page + 1; candidate += 1) {
    if (candidate > 1 && candidate < pageCount) candidates.add(candidate);
  }

  const pages = [...candidates].sort((left, right) => left - right);
  const tokens: PageToken[] = [];
  pages.forEach((candidate, index) => {
    const previous = pages[index - 1];
    if (previous !== undefined && candidate - previous > 1) {
      tokens.push(previous === 1 ? 'ellipsis-start' : 'ellipsis-end');
    }
    tokens.push(candidate);
  });
  return tokens;
}

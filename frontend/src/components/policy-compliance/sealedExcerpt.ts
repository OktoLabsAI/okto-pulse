/** Presentation only: never changes the sealed evidence or its digest. */
export function readableSealedExcerpt(excerpt: string): string {
  try {
    const value: unknown = JSON.parse(excerpt);
    if (!value || typeof value !== 'object' || Array.isArray(value)) return excerpt;
    const record = value as Record<string, unknown>;
    const content = ['text', 'description', 'content', 'question', 'answer']
      .flatMap(key => typeof record[key] === 'string' && record[key].trim()
        ? [record[key] as string] : []);
    if (content.length === 0) return excerpt;
    const title = typeof record.title === 'string' ? record.title.trim() : '';
    return [title, ...content].filter(Boolean).join('\n\n');
  } catch {
    // Plain text and bounded/truncated excerpts remain literal.
    return excerpt;
  }
}

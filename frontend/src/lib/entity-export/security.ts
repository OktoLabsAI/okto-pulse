/** Fail closed if a server-generated HTML attachment is not passive. */
export function assertPassiveStandaloneHtml(html: string): void {
  const parsed = new DOMParser().parseFromString(html, 'text/html');
  if (parsed.querySelector('script')) {
    throw new Error('The HTML report was blocked because it contains a script element.');
  }
  if (parsed.querySelector('iframe,frame,frameset,object,embed,applet,base,meta[http-equiv="refresh" i]')) {
    throw new Error('The HTML report was blocked because it contains active embedded content.');
  }
  if (parsed.querySelector('link[href],link[rel]')) {
    throw new Error('The HTML report was blocked because it contains an external link resource.');
  }
  for (const element of Array.from(parsed.querySelectorAll('*'))) {
    for (const attribute of Array.from(element.attributes)) {
      const name = attribute.name.toLowerCase();
      const value = attribute.value.trim();
      if (/^on[a-z]/.test(name)) {
        throw new Error('The HTML report was blocked because it contains an inline event handler.');
      }
      if (
        ['src', 'poster'].includes(name)
        && /^(?:https?:|\/\/)/i.test(value)
      ) {
        throw new Error('The HTML report was blocked because it contains an external asset URL.');
      }
      if (
        ['href', 'src', 'action', 'formaction'].includes(name)
        && /^(?:javascript|vbscript)\s*:/i.test(value)
      ) {
        throw new Error('The HTML report was blocked because it contains an executable URL.');
      }
      if (
        ['href', 'src'].includes(name)
        && /^data\s*:\s*text\/html/i.test(value)
      ) {
        throw new Error('The HTML report was blocked because it contains an active data URL.');
      }
      if (
        name === 'style'
        && /@import\s|url\(\s*["']?\s*(?:https?:|\/\/)/i.test(value)
      ) {
        throw new Error('The HTML report was blocked because it contains an external CSS resource.');
      }
    }
  }
  for (const style of Array.from(parsed.querySelectorAll('style'))) {
    if (/@import\s|url\(\s*["']?\s*(?:https?:|\/\/)/i.test(style.textContent || '')) {
      throw new Error('The HTML report was blocked because it contains an external CSS resource.');
    }
  }
}

export function escapeHtml(value: unknown): string {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function safeDownloadFilename(
  candidate: string | null | undefined,
  fallback: string,
): string {
  const rawLeaf = String(candidate || '')
    .replace(/\\/g, '/')
    .split('/')
    .at(-1) ?? '';
  const withoutControlCharacters = Array.from(rawLeaf)
    .filter((character) => {
      const codePoint = character.codePointAt(0) ?? 0;
      return codePoint >= 32 && codePoint !== 127;
    })
    .join('');
  const leaf = withoutControlCharacters
    .replace(/[^A-Za-z0-9._() -]+/g, '-')
    .replace(/^\.+/, '')
    .trim();
  return leaf || fallback;
}

export function contentDispositionFilename(header: string | null): string | null {
  if (!header) return null;
  const encoded = header.match(/filename\*\s*=\s*UTF-8''([^;]+)/i)?.[1];
  if (encoded) {
    try {
      return decodeURIComponent(encoded.replace(/^"|"$/g, ''));
    } catch {
      return null;
    }
  }
  return header.match(/filename\s*=\s*"([^"]+)"/i)?.[1]
    ?? header.match(/filename\s*=\s*([^;\s]+)/i)?.[1]
    ?? null;
}

export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  try {
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
  } finally {
    anchor.remove();
    URL.revokeObjectURL(url);
  }
}

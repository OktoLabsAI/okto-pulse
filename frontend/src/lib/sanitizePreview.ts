/**
 * Sanitize a preview string for card list display.
 *
 * NC-3 fix: ideation/spec card list previews used to render incoming
 * descriptions verbatim. When a tool_use formatter accidentally
 * concatenated XML-like tags into the field
 * (`</problem_statement>...<proposed_approach>...</invoke>`), React
 * escaped the angle brackets but the literal markup still polluted the
 * UI. This helper strips obvious tag/markup noise and trims excess
 * whitespace so the preview stays readable.
 *
 * Defensive only — never assume the input is sanitized at render time.
 * For HTML output use a real escaper / DOMPurify; this is text-only.
 */
const TAG_PATTERN = /<\/?[a-zA-Z][a-zA-Z0-9_-]*(\s+[^>]*)?>/g;
const INVOKE_NOISE = /<\/?(invoke|antml:[a-z_]+|tool_use|parameter)(\s+[^>]*)?>/gi;
const ATTRIBUTE_PATTERN = /\s+[a-zA-Z_-]+="[^"]*"/g;

export function sanitizePreview(input: string | null | undefined, maxLength = 280): string {
  if (!input) return '';
  let cleaned = input;
  cleaned = cleaned.replace(INVOKE_NOISE, ' ');
  cleaned = cleaned.replace(TAG_PATTERN, ' ');
  cleaned = cleaned.replace(ATTRIBUTE_PATTERN, ' ');
  cleaned = cleaned.replace(/\s+/g, ' ').trim();
  if (cleaned.length > maxLength) {
    cleaned = `${cleaned.slice(0, maxLength).trim()}…`;
  }
  return cleaned;
}

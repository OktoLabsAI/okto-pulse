import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { MarkdownContent } from '../MarkdownContent';

describe('MarkdownContent', () => {
  it('renders markdown headings as heading elements', () => {
    render(<MarkdownContent content={'# Title One\n\n## Title Two'} />);
    expect(screen.getByRole('heading', { level: 1, name: 'Title One' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 2, name: 'Title Two' })).toBeInTheDocument();
  });

  it('renders bold text as <strong>', () => {
    render(<MarkdownContent content={'This is **important** stuff'} />);
    const el = screen.getByText('important');
    expect(el.tagName).toBe('STRONG');
  });

  it('renders unordered and ordered lists as list items', () => {
    render(<MarkdownContent content={'- alpha\n- beta\n\n1. one\n2. two'} />);
    const items = screen.getAllByRole('listitem');
    expect(items.map((li) => li.textContent)).toEqual(['alpha', 'beta', 'one', 'two']);
  });

  it('renders GFM tables as table elements', () => {
    const md = '| Col A | Col B |\n| --- | --- |\n| a1 | b1 |';
    render(<MarkdownContent content={md} />);
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Col A' })).toBeInTheDocument();
    expect(screen.getByRole('cell', { name: 'b1' })).toBeInTheDocument();
  });

  it('renders inline and fenced code as <code> elements', () => {
    const { container } = render(
      <MarkdownContent content={'Use `npm ci` here\n\n```js\nconst x = 1;\n```'} />,
    );
    const codes = Array.from(container.querySelectorAll('code'));
    expect(codes.some((c) => c.textContent === 'npm ci')).toBe(true);
    expect(codes.some((c) => (c.textContent || '').includes('const x = 1;'))).toBe(true);
  });

  it('does not render raw HTML from untrusted content', () => {
    const { container } = render(
      <MarkdownContent
        content={'<script>window.__pwned = true;</script>\n\n<b>injected</b> safe text'}
      />,
    );
    // Raw HTML must never become live DOM elements (agent content is untrusted).
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
    expect((window as unknown as { __pwned?: boolean }).__pwned).toBeUndefined();
    // The surrounding text survives as plain text.
    expect(screen.getByText(/injected/)).toBeInTheDocument();
  });

  it('appends the provided className to the prose wrapper', () => {
    const { container } = render(<MarkdownContent content={'plain'} className="text-xs" />);
    const wrapper = container.firstElementChild as HTMLElement;
    expect(wrapper.className).toContain('prose');
    expect(wrapper.className).toContain('text-xs');
  });
});

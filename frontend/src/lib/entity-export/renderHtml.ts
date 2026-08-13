import { escapeHtml } from './security';
import type {
  ExportReportBlock,
  ExportReportDocument,
  ExportReportSection,
} from './reportDocument';

function domId(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '') || 'section';
}

function renderBlock(block: ExportReportBlock): string {
  switch (block.kind) {
    case 'paragraph':
      return `<p>${escapeHtml(block.text)}</p>`;
    case 'key_values':
      return `<dl class="key-values">${block.items.map((item) => (
        `<div><dt>${escapeHtml(item.key)}</dt><dd>${escapeHtml(item.value)}</dd></div>`
      )).join('')}</dl>`;
    case 'list': {
      const tag = block.ordered ? 'ol' : 'ul';
      return `<${tag}>${block.items.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</${tag}>`;
    }
    case 'table':
      return `<div class="table-wrap"><table>${block.caption ? `<caption>${escapeHtml(block.caption)}</caption>` : ''}<thead><tr>${block.columns.map((column) => `<th scope="col">${escapeHtml(column)}</th>`).join('')}</tr></thead><tbody>${block.rows.map((row) => `<tr>${block.columns.map((_, index) => `<td>${escapeHtml(row[index] ?? '')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    case 'metrics':
      return `<div class="metrics">${block.metrics.map((metric) => {
        const maximum = Math.max(1, metric.maximum);
        const value = Math.max(0, Math.min(maximum, metric.value));
        const ratio = value / maximum;
        const offset = (100 - ratio * 100).toFixed(2);
        return `<article class="metric"><svg viewBox="0 0 42 42" role="img" aria-label="${escapeHtml(`${metric.label}: ${metric.value} of ${metric.maximum}`)}"><circle class="metric-track" cx="21" cy="21" r="15.9155"></circle><circle class="metric-value" cx="21" cy="21" r="15.9155" stroke-dasharray="100" stroke-dashoffset="${offset}"></circle></svg><strong>${escapeHtml(metric.value)}/${escapeHtml(metric.maximum)}</strong><span>${escapeHtml(metric.label)}</span>${metric.threshold == null ? '' : `<small>${metric.direction === 'lower' ? 'Maximum' : 'Minimum'} ${escapeHtml(metric.threshold)}</small>`}${metric.justification ? `<p>${escapeHtml(metric.justification)}</p>` : ''}</article>`;
      }).join('')}</div>`;
    case 'pinpoints':
      return `<ol class="pinpoints">${block.items.map((item) => `<li><div><span class="metric-tag">${escapeHtml(item.metric)}</span><strong>${escapeHtml(item.target)}</strong>${item.target_id ? ` <code>(${escapeHtml(item.target_id)})</code>` : ''}</div><p>${escapeHtml(item.detail)}</p></li>`).join('')}</ol>`;
    case 'code':
      return `<pre><code data-language="${escapeHtml(block.language || '')}">${escapeHtml(block.value)}</code></pre>`;
    case 'details':
      return `<details${block.open ? ' open' : ''}><summary>${escapeHtml(block.summary)}</summary><div class="details-body">${block.blocks.map(renderBlock).join('')}</div></details>`;
  }
}

function renderSection(section: ExportReportSection): string {
  const state = section.state
    ? `<span class="state state-${escapeHtml(section.state)}">${escapeHtml(section.state.replace(/_/g, ' '))}</span>`
    : '';
  return `<section id="${domId(section.id)}" data-semantic-key="section:${escapeHtml(section.id)}"><header class="section-heading"><div><p class="eyebrow">Report section</p><h2>${escapeHtml(section.title)}</h2></div>${state}</header>${section.summary ? `<p class="section-summary">${escapeHtml(section.summary)}</p>` : ''}${section.blocks.map(renderBlock).join('')}</section>`;
}

const REPORT_CSS = `
:root{color-scheme:light dark;--bg:#f4f7fb;--surface:#fff;--surface-2:#f7f9fc;--text:#172033;--muted:#657087;--line:#dbe2ec;--accent:#7657e8;--ok:#087f5b;--warn:#a15c00;--bad:#b42318;font:15px/1.55 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text)}a{color:inherit}.shell{max-width:1180px;margin:auto;padding:32px}.hero{background:linear-gradient(135deg,#211d3d,#353060);color:#fff;border-radius:18px;padding:30px;box-shadow:0 18px 50px #16122c2e}.hero .eyebrow{color:#cfc5ff}.hero h1{font-size:2rem;margin:.2rem 0}.hero p{color:#ddd7ff}.snapshot{display:inline-block;margin-top:12px;padding:5px 9px;border:1px solid #ffffff30;border-radius:7px;font:11px ui-monospace,monospace;overflow-wrap:anywhere}.layout{display:grid;grid-template-columns:230px minmax(0,1fr);gap:22px;margin-top:22px}.toc{position:sticky;top:16px;align-self:start;background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:16px}.toc h2{font-size:.8rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}.toc ol{padding-left:20px}.toc li{margin:8px 0}.content{min-width:0}section{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:22px;margin-bottom:18px;break-inside:avoid}.section-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}.eyebrow{margin:0;color:var(--accent);font-size:.7rem;text-transform:uppercase;letter-spacing:.1em;font-weight:700}.section-heading h2{margin:2px 0 12px;font-size:1.3rem}.section-summary{color:var(--muted);margin-top:0}.state{border-radius:999px;padding:4px 9px;font-size:.68rem;font-weight:700;text-transform:uppercase;white-space:nowrap;background:#e7e9ee}.state-included{background:#d8f5e8;color:var(--ok)}.state-omitted,.state-unavailable{background:#fff0cf;color:var(--warn)}.state-error{background:#fee4e2;color:var(--bad)}.key-values{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.key-values div{background:var(--surface-2);border:1px solid var(--line);border-radius:9px;padding:10px}.key-values dt{font-size:.7rem;text-transform:uppercase;color:var(--muted)}.key-values dd{margin:2px 0 0;font-weight:650}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:14px;margin:16px 0}.metric{position:relative;display:grid;justify-items:center;text-align:center;border:1px solid var(--line);border-radius:12px;padding:14px}.metric svg{width:84px;transform:rotate(-90deg)}.metric circle{fill:none;stroke-width:4}.metric-track{stroke:var(--line)}.metric-value{stroke:var(--accent);stroke-linecap:round}.metric strong{font-size:1.2rem;margin-top:-56px}.metric span{font-weight:700;margin-top:34px}.metric small,.metric p{color:var(--muted)}.metric p{font-size:.75rem}.table-wrap{overflow-x:auto;margin-top:14px}table{width:100%;border-collapse:collapse;font-size:.85rem}caption{text-align:left;color:var(--muted);padding-bottom:8px}th,td{text-align:left;border-bottom:1px solid var(--line);padding:9px;vertical-align:top}th{font-size:.7rem;text-transform:uppercase;color:var(--muted)}.pinpoints{list-style:none;padding:0}.pinpoints li{border:1px solid var(--line);border-radius:10px;padding:12px;margin:9px 0}.metric-tag{display:inline-block;background:#eee9ff;color:#5c3fc0;border-radius:999px;padding:3px 7px;margin-right:8px;font-size:.65rem;font-weight:800;text-transform:uppercase}.pinpoints code{color:var(--muted);font-size:.75rem}.pinpoints p{margin:8px 0 0}pre{overflow:auto;background:#141827;color:#edf0ff;border-radius:10px;padding:14px}details{border-top:1px solid var(--line);padding-top:10px}summary{cursor:pointer;font-weight:700}.details-body{padding:10px 2px}@media(prefers-color-scheme:dark){:root{--bg:#111725;--surface:#1b2434;--surface-2:#151d2a;--text:#eef2fa;--muted:#aab4c6;--line:#344157}.state{color:#172033}}@media(max-width:760px){.shell{padding:14px}.layout{display:block}.toc{position:static;margin-bottom:16px}.key-values{grid-template-columns:1fr}.hero{padding:22px}}@media print{:root{color-scheme:light;--bg:#fff;--surface:#fff;--surface-2:#f7f7f7;--text:#111;--muted:#555;--line:#ccc}.shell{max-width:none;padding:0}.hero{box-shadow:none;color:#111;background:#fff;border:2px solid #222}.hero p,.hero .eyebrow{color:#444}.snapshot{border-color:#bbb}.layout{display:block}.toc{position:static;page-break-after:always}.content section{border-radius:0;box-shadow:none;break-inside:auto}details>:not(summary){display:block!important}.metric{break-inside:avoid}}
`;

export function renderReportHtml(document: ExportReportDocument): string {
  const toc = document.sections.map((section) => `<li><a href="#${domId(section.id)}">${escapeHtml(section.title)}</a></li>`).join('');
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src 'none'; connect-src 'none'; form-action 'none'; base-uri 'none'"><title>${escapeHtml(document.title)}</title><style>${REPORT_CSS}</style></head><body><main class="shell"><header class="hero"><p class="eyebrow">Okto Pulse report</p><h1>${escapeHtml(document.title)}</h1>${document.subtitle ? `<p>${escapeHtml(document.subtitle)}</p>` : ''}${document.snapshot_fingerprint ? `<span class="snapshot">Snapshot ${escapeHtml(document.snapshot_fingerprint)}</span>` : ''}</header><div class="layout"><nav class="toc" aria-label="Report contents"><h2>Contents</h2><ol>${toc}</ol></nav><div class="content">${document.sections.map(renderSection).join('')}</div></div></main></body></html>`;
}

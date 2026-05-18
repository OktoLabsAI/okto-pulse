import { expect, test } from '@playwright/test';
import {
  ARCHITECTURE_COMPONENT_SEGMENTS,
  resolveArchitectureVisualStyle,
  type ArchitectureVisualTheme,
} from '../../src/components/architecture/architectureVisualRegistry';

const MATRIX_TYPES = [
  'AWS Lambda',
  'AWS API Gateway',
  'AWS S3',
  'AWS SQS',
  'AWS CloudFront',
  'AWS Cognito',
  'Azure Functions',
  'Azure API Management',
  'Azure Blob Storage',
  'GCP Cloud Run',
  'GCP Cloud Storage',
  'GCP Pub/Sub Topic',
  'Service',
  'Database',
  'Dead Letter Queue',
  'Observability',
];

function presetIcon(type: string) {
  return ARCHITECTURE_COMPONENT_SEGMENTS
    .flatMap((segment) => segment.items)
    .find((preset) => preset.entityType === type)?.icon;
}

function renderMatrix(theme: ArchitectureVisualTheme) {
  const background = theme === 'dark' ? '#020617' : '#f8fafc';
  const panel = theme === 'dark' ? '#0f172a' : '#ffffff';
  const border = theme === 'dark' ? '#334155' : '#cbd5e1';
  const heading = theme === 'dark' ? '#f8fafc' : '#0f172a';
  const subtext = theme === 'dark' ? '#94a3b8' : '#475569';
  const rows = MATRIX_TYPES.map((type) => {
    const tokens = resolveArchitectureVisualStyle({
      theme,
      displayType: type,
      architectureKind: type,
      iconName: presetIcon(type),
      strokeColor: theme === 'dark' && type === 'AWS Lambda' ? '#f59e0b' : undefined,
      backgroundColor: theme === 'dark' && type === 'AWS Lambda' ? '#fffbeb' : undefined,
    });

    return `
      <div class="node" style="border-color:${tokens.stroke};background:${tokens.fill};color:${tokens.text}">
        <div class="glyph" style="border-color:${tokens.stroke};color:${tokens.stroke}">${tokens.icon.slice(0, 2).toUpperCase()}</div>
        <div class="copy">
          <div class="title">${type}</div>
          <div class="meta" style="color:${tokens.mutedText}">${tokens.source} · ${tokens.icon}</div>
        </div>
      </div>
    `;
  }).join('');

  return `
    <!doctype html>
    <html class="${theme === 'dark' ? 'dark' : ''}">
      <head>
        <meta charset="utf-8" />
        <style>
          * { box-sizing: border-box; }
          body {
            margin: 0;
            min-height: 100vh;
            background: ${background};
            color: ${heading};
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          }
          .wrap {
            width: 1120px;
            padding: 28px;
          }
          .header {
            display: flex;
            align-items: flex-end;
            justify-content: space-between;
            margin-bottom: 18px;
          }
          h1 {
            margin: 0;
            font-size: 22px;
            line-height: 1.2;
            font-weight: 700;
            letter-spacing: 0;
          }
          .caption {
            margin-top: 5px;
            color: ${subtext};
            font-size: 13px;
          }
          .theme {
            border: 1px solid ${border};
            border-radius: 8px;
            padding: 6px 10px;
            background: ${panel};
            color: ${heading};
            font-size: 12px;
            font-weight: 700;
            text-transform: uppercase;
          }
          .matrix {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 14px;
            padding: 18px;
            border: 1px solid ${border};
            border-radius: 8px;
            background: ${panel};
          }
          .node {
            min-height: 76px;
            display: flex;
            align-items: center;
            gap: 12px;
            border: 2px solid;
            border-radius: 8px;
            padding: 12px;
            box-shadow: 0 8px 20px rgba(15, 23, 42, ${theme === 'dark' ? '0.18' : '0.08'});
          }
          .glyph {
            width: 34px;
            height: 34px;
            flex: 0 0 auto;
            display: grid;
            place-items: center;
            border: 1px solid;
            border-radius: 7px;
            font-size: 11px;
            font-weight: 800;
          }
          .copy {
            min-width: 0;
          }
          .title {
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            font-size: 14px;
            font-weight: 700;
          }
          .meta {
            margin-top: 4px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
          }
        </style>
      </head>
      <body>
        <main class="wrap" data-testid="architecture-visual-matrix">
          <div class="header">
            <div>
              <h1>Architecture Visual Resolver Matrix</h1>
              <div class="caption">Semantic presets, custom color fallback and readable tokens.</div>
            </div>
            <div class="theme">${theme} mode</div>
          </div>
          <section class="matrix">${rows}</section>
        </main>
      </body>
    </html>
  `;
}

test.describe('Architecture visual resolver screenshots', () => {
  for (const theme of ['light', 'dark'] as const) {
    test(`renders semantic matrix in ${theme} mode`, async ({ page }) => {
      await page.setViewportSize({ width: 1180, height: 540 });
      await page.setContent(renderMatrix(theme));

      await expect(page.getByTestId('architecture-visual-matrix')).toHaveScreenshot(
        `architecture-visual-registry-${theme}.png`,
        { maxDiffPixelRatio: 0.01 },
      );
    });
  }
});

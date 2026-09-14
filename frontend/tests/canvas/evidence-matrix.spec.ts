import { test, expect } from '@playwright/test';

test('contextual TR reference stays visible without becoming implementation coverage', async ({ page }) => {
  await page.route('**/api/v1/**', async route => {
    if (!route.request().url().includes('/code-traceability-projection?')) {
      await route.fulfill({ status: 404, json: { detail: 'Isolated fixture' } });
      return;
    }
    await route.fulfill({ json: {
      inherited_evidence_ids: ['e-1'],
      evidence: [{ id: 'e-1', claim: 'Native history is scoped to the reader snapshot.',
        relative_path: 'docs/history.md', investigation_receipt_id: 'r-1' }],
      dispositions: [], omitted_content_manifest: [],
      source_context: { delivery_context: 'greenfield', evidence_applicable: null },
      source_context_items: [{ evidence_id: 'e-1', evidence_applicable: false }],
      contextual_evidence_coverage: { total: 0, linked: 0, dispositioned: 0, pending: 0,
        pending_ids: [], unresolved_applicability_count: 0, coverage_pct: null, projection_complete: true },
      gate_readiness: { evidence_coverage_skipped: false, receipt_currentness: {} },
      obligation_evidence_mappings: [{ link_id: 'l-1', evidence_id: 'e-1',
        obligation_type: 'technical_requirement', obligation_id: 'tr-history',
        obligation_ref: 'technical_requirement:tr-history', relation_type: 'constrains',
        evidence_applicable: false, source_role: 'existing_constraint' }],
    } });
  });
  await page.goto('/tests/fixtures/evidence-matrix.html');
  const row = page.getByRole('row').filter({ hasText: 'Native history is scoped' });
  await expect(row.getByRole('cell').nth(2)).toContainText('Preserve native snapshot boundaries');
  await expect(row.getByRole('cell').nth(2)).toContainText('Contextual link');
  await expect(row.getByRole('cell').nth(1)).toHaveText('—');
  await expect(page.getByTestId('contextual-evidence-linked')).toHaveText('0');
  await expect(page.getByTestId('contextual-evidence-coverage-pct')).toHaveText('—');
  await expect(page.getByText('Context only', { exact: true })).toBeVisible();
  await page.screenshot({ path: 'test-results/evidence-matrix-contextual.png', fullPage: true });
});

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import type { DeliveryEvidenceProjection } from '@/types/delivery-evidence';
import { DeliveryExceptionDetails } from '../DeliveryExceptionDetails';

afterEach(cleanup);
const data = { edition: 2, records: [{ id: 'waiver', kind: 'waiver', actor_id: 'reviewer', created_at: '2026-10-14T10:00:00Z',
  revoked: true, disposition: 'risk_accepted', payload: { justification: '<script>Accepted bounded risk</script>', phase: 'test',
    bindings: [{ obligation_ref: 'fr:capacity', semantic_sha256: 'a'.repeat(64) }] } }] } as DeliveryEvidenceProjection;

it('preserves exception scope and revocation without presenting it as proof', () => {
  const { container } = render(<DeliveryExceptionDetails data={data} recordId="waiver" />);
  expect(screen.getByRole('article')).toHaveAttribute('data-readiness-ref', 'exception:waiver');
  expect(screen.getByText('Delivery exception · Revoked')).toBeInTheDocument();
  expect(screen.getByText(/not implementation or verification proof/)).toBeInTheDocument();
  expect(screen.getByText(/fr:capacity/)).toBeInTheDocument();
  expect(container.querySelector('script')).toBeNull();
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
});

it('does not silently infer a historical disposition or substitute another record', () => {
  const { rerender } = render(<DeliveryExceptionDetails data={{ ...data, records: [{ ...data.records[0], legacy_disposition_unspecified: true }] }} recordId="waiver" />);
  expect(screen.getByText('Unspecified in the original record')).toBeInTheDocument();
  rerender(<DeliveryExceptionDetails data={{ ...data, records: [{ ...data.records[0], kind: 'test' }] }} recordId="waiver" />);
  expect(screen.getByRole('alert')).toHaveTextContent('unavailable');
  expect(screen.queryByRole('article')).not.toBeInTheDocument();
});

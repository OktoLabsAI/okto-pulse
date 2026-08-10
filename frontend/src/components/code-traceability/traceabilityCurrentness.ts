import type {
  CodeTraceabilityProjection,
  CodeTraceabilityReceiptCurrentness,
} from '@/types';

export function projectedReceiptCurrentness(
  projection: CodeTraceabilityProjection,
  receiptId: string,
): CodeTraceabilityReceiptCurrentness {
  return projection.gate_readiness.receipt_currentness?.[receiptId] ?? 'unknown';
}

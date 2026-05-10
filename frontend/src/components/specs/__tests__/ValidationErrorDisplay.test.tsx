import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ValidationErrorDisplay, parseValidationErrorMessage } from '../ValidationErrorDisplay';

describe('ValidationErrorDisplay', () => {
  it('renders structured Resource Gate uncovered resources', () => {
    const error = JSON.stringify({
      error: 'resource_gate_spec_task_coverage',
      message: 'Cannot advance spec: mandatory spec resources are not covered by non-cancelled task cards.',
      details: {
        uncovered_resources: [
          {
            resource_type: 'architecture',
            resource_id: 'arch-1',
            resource_title: 'Resource Gate architecture',
            source_entity_type: 'spec',
            source_entity_title: 'Resource Gate spec',
            reason: 'uncovered',
            remediation: 'Attach or copy this resource directly to at least one non-cancelled task.',
          },
        ],
      },
    });

    render(<ValidationErrorDisplay error={error} />);

    expect(screen.getByText('Resource Coverage')).toBeInTheDocument();
    expect(screen.getByText(/Resource Gate architecture/)).toBeInTheDocument();
    expect(screen.getByText('Uncovered')).toBeInTheDocument();
    expect(screen.getByText(/Attach or copy this resource directly/)).toBeInTheDocument();
  });

  it('keeps legacy REQUIRED ACTION parsing', () => {
    const parsed = parseValidationErrorMessage(
      'Cannot validate spec: 1 test scenario is uncovered. REQUIRED ACTION: Link a test task.',
    );

    expect(parsed.gateType).toBe('Test Coverage');
    expect(parsed.issue).toContain('uncovered');
    expect(parsed.action).toBe('Link a test task.');
  });
});

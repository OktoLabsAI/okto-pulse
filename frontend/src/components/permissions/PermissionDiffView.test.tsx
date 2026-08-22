import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { PermissionDiffView } from './PermissionDiffView';
import { getEntityTextClasses } from './permissionLabels';

describe('PermissionDiffView', () => {
  it('compares exact base and effective leaves even when enabled counts cancel', () => {
    render(
      <PermissionDiffView
        baseFlags={{
          board: {
            entity: {
              read: true,
              update: false,
            },
          },
        }}
        effectiveFlags={{
          board: {
            entity: {
              read: false,
              update: true,
            },
          },
        }}
        baseLabel="Full Control"
        restrictionLabel="direct agent customization"
      />,
    );

    expect(screen.getByTestId('permission-diff-base')).toHaveTextContent(
      'Base: Full Control',
    );
    expect(screen.getByTestId('permission-diff-summary')).toHaveTextContent(
      '1 flag restricted by direct agent customization',
    );
    expect(screen.getByTestId('permission-diff-summary')).toHaveTextContent(
      '1 flag enabled by direct customization',
    );
    expect(screen.queryByText(/No effective changes/)).not.toBeInTheDocument();
  });

  it('uses the shared light and dark palette for newly introduced policy groups', () => {
    render(
      <PermissionDiffView
        baseFlags={{ permission_preset: { read: true } }}
        effectiveFlags={{ permission_preset: { read: false } }}
      />,
    );

    expect(screen.getByText('Permission Presets', { exact: true })).toHaveClass(
      ...getEntityTextClasses('permission_preset').split(' '),
    );
  });
});

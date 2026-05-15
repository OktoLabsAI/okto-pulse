import { describe, expect, it } from 'vitest';
import {
  ARCHITECTURE_COMPONENT_SEGMENTS,
  colorForArchitectureType,
  contrastRatio,
  findArchitecturePreset,
  iconForArchitectureType,
  resolveArchitectureVisualStyle,
} from '../architectureVisualRegistry';

describe('architectureVisualRegistry', () => {
  it('keeps AWS presets in the shared registry', () => {
    const awsSegment = ARCHITECTURE_COMPONENT_SEGMENTS.find((segment) => segment.id === 'aws');
    const ids = new Set(awsSegment?.items.map((item) => item.id));

    expect(ids.has('aws-lambda')).toBe(true);
    expect(ids.has('aws-api-gateway')).toBe(true);
    expect(ids.has('aws-s3')).toBe(true);
    expect(ids.has('aws-cloudfront')).toBe(true);
  });

  it('resolves aliases and helper values from the same registry', () => {
    expect(findArchitecturePreset('Lambda')?.entityType).toBe('AWS Lambda');
    expect(colorForArchitectureType('AWS API Gateway')).toBe('#f59e0b');
    expect(iconForArchitectureType('AWS S3')).toBe('hard_drive');
  });

  it('keeps every catalog preset resolvable by id, label and entity type', () => {
    const seen = new Set<string>();
    const labelCounts = ARCHITECTURE_COMPONENT_SEGMENTS
      .flatMap((segment) => segment.items)
      .reduce<Record<string, number>>((acc, preset) => {
        const key = preset.label.toLowerCase();
        acc[key] = (acc[key] || 0) + 1;
        return acc;
      }, {});

    for (const segment of ARCHITECTURE_COMPONENT_SEGMENTS) {
      for (const preset of segment.items) {
        expect(seen.has(preset.id)).toBe(false);
        seen.add(preset.id);
        expect(findArchitecturePreset(preset.id)?.id).toBe(preset.id);
        if (labelCounts[preset.label.toLowerCase()] === 1) {
          expect(findArchitecturePreset(preset.label)?.id).toBe(preset.id);
        }
        expect(findArchitecturePreset(preset.entityType)?.id).toBe(preset.id);
        expect(colorForArchitectureType(preset.entityType)).toBe(preset.color);
        expect(iconForArchitectureType(preset.entityType)).toBe(preset.icon);
      }
    }
  });

  it('resolves common cloud and generic aliases', () => {
    expect(findArchitecturePreset('Cognito')?.id).toBe('aws-cognito');
    expect(findArchitecturePreset('APIM')?.id).toBe('azure-apim');
    expect(findArchitecturePreset('GCS')?.id).toBe('gcp-storage');
    expect(findArchitecturePreset('BigQuery')?.id).toBe('gcp-bigquery');
    expect(findArchitecturePreset('dead letter queue')?.id).toBe('dlq');
    expect(findArchitecturePreset('backend for frontend')?.id).toBe('bff');
  });

  it('provides readable dark-mode tokens for the full catalog', () => {
    for (const segment of ARCHITECTURE_COMPONENT_SEGMENTS) {
      for (const preset of segment.items) {
        const tokens = resolveArchitectureVisualStyle({
          theme: 'dark',
          displayType: preset.entityType,
          iconName: preset.icon,
        });

        expect(tokens.matchedType).toBe(preset.entityType);
        expect(tokens.icon).toBe(preset.icon);
        expect(contrastRatio(tokens.fill, tokens.text) ?? 0).toBeGreaterThanOrEqual(4.5);
        expect(contrastRatio(tokens.stroke, '#020617') ?? 0).toBeGreaterThanOrEqual(3);
      }
    }
  });

  it('returns dark-mode tokens with readable contrast for AWS compute nodes', () => {
    const tokens = resolveArchitectureVisualStyle({
      theme: 'dark',
      displayType: 'AWS Lambda',
      architectureKind: 'aws-lambda',
      iconName: 'cloud',
    });

    expect(tokens.icon).toBe('cloud');
    expect(tokens.source).toBe('semantic');
    expect(tokens.fill).not.toBe('#f59e0b');
    expect(contrastRatio(tokens.fill, tokens.text) ?? 0).toBeGreaterThanOrEqual(4.5);
  });

  it('falls back when a custom fill would make text unreadable', () => {
    const tokens = resolveArchitectureVisualStyle({
      theme: 'dark',
      displayType: 'AWS Lambda',
      strokeColor: '#f59e0b',
      backgroundColor: '#fffbeb',
    });

    expect(tokens.source).toBe('fallback');
    expect(tokens.stroke).toBe('#fbbf24');
    expect(tokens.fill).not.toBe('#fffbeb');
    expect(contrastRatio(tokens.fill, tokens.text) ?? 0).toBeGreaterThanOrEqual(4.5);
  });

  it('preserves safe custom colors', () => {
    const tokens = resolveArchitectureVisualStyle({
      theme: 'light',
      displayType: 'Service',
      strokeColor: '#111827',
      backgroundColor: '#ffffff',
    });

    expect(tokens.source).toBe('custom');
    expect(tokens.stroke).toBe('#111827');
  });
});

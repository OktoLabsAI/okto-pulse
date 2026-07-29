import type { FlagsMap } from './PermissionFlagsEditor';

export const SKA_PERMISSION_INTRODUCTION_V1_LEAVES = [
  'ideation.quality.read',
  'ideation.quality.assess',
  'refinement.quality.read',
  'refinement.quality.assess',
  'spec.quality.read',
  'spec.quality.assess',
  'refinement.research_decisions.read',
  'refinement.research_decisions.append',
  'spec.checklist.read',
  'spec.checklist.execute',
] as const;

type PermissionDocument = Record<string, unknown>;

function isDocument(value: unknown): value is PermissionDocument {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function cloneDocument<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function isStrictPermissionDocument(value: unknown): value is PermissionDocument {
  if (!isDocument(value)) return false;
  return Object.values(value).every(
    (child) => typeof child === 'boolean'
      || (isDocument(child) && isStrictPermissionDocument(child)),
  );
}

function setNested(
  document: PermissionDocument,
  path: string,
  value: boolean,
): void {
  const parts = path.split('.');
  let current = document;
  for (const part of parts.slice(0, -1)) {
    const child = current[part];
    if (!isDocument(child)) {
      current[part] = {};
    }
    current = current[part] as PermissionDocument;
  }
  current[parts.at(-1)!] = value;
}

function getNested(
  document: PermissionDocument,
  path: string,
): { present: boolean; value: unknown } {
  let current: unknown = document;
  for (const part of path.split('.')) {
    if (!isDocument(current) || !(part in current)) {
      return { present: false, value: undefined };
    }
    current = current[part];
  }
  return { present: true, value: current };
}

function booleanLeaves(
  document: PermissionDocument,
  prefix = '',
): Array<[string, boolean]> {
  const leaves: Array<[string, boolean]> = [];
  for (const [key, value] of Object.entries(document)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (typeof value === 'boolean') {
      leaves.push([path, value]);
    } else if (isDocument(value)) {
      leaves.push(...booleanLeaves(value, path));
    }
  }
  return leaves;
}

export function allPermissionsDenied(
  source: PermissionDocument,
): PermissionDocument {
  const denied: PermissionDocument = {};
  for (const [path] of booleanLeaves(source)) {
    setNested(denied, path, false);
  }
  return denied;
}

function overlayInto(
  target: PermissionDocument,
  overrides: PermissionDocument,
): void {
  for (const [key, value] of Object.entries(overrides)) {
    if (typeof value === 'boolean') {
      target[key] = value;
    } else if (isDocument(value)) {
      const current = isDocument(target[key]) ? target[key] : {};
      target[key] = current;
      overlayInto(current, value);
    }
  }
}

/** Apply a sparse direct agent delta over its resolved preset/Full Control base. */
export function applyPermissionDelta(
  base: PermissionDocument,
  delta: unknown,
): FlagsMap {
  if (!isStrictPermissionDocument(base)) return {} as FlagsMap;
  if (delta !== null && delta !== undefined && !isStrictPermissionDocument(delta)) {
    return allPermissionsDenied(base) as FlagsMap;
  }
  const effective = cloneDocument(base);
  if (isStrictPermissionDocument(delta)) overlayInto(effective, delta);
  return effective as FlagsMap;
}

/** Compute the sparse direct agent delta for a desired effective document. */
export function permissionDelta(
  base: PermissionDocument,
  desired: PermissionDocument,
): PermissionDocument {
  const result: PermissionDocument = {};
  for (const [key, desiredValue] of Object.entries(desired)) {
    const baseValue = base[key];
    if (typeof desiredValue === 'boolean') {
      if (desiredValue !== baseValue) result[key] = desiredValue;
    } else if (isDocument(desiredValue)) {
      const nested = permissionDelta(
        isDocument(baseValue) ? baseValue : {},
        desiredValue,
      );
      if (Object.keys(nested).length > 0) result[key] = nested;
    }
  }
  return result;
}

/** Apply the raw board ceiling with the same missing-introduced-leaf rule as Core. */
export function applyBoardCeiling(
  base: PermissionDocument,
  ceiling: unknown,
): FlagsMap {
  if (ceiling === null || ceiling === undefined) {
    return cloneDocument(base) as FlagsMap;
  }
  if (!isStrictPermissionDocument(ceiling)) {
    return allPermissionsDenied(base) as FlagsMap;
  }

  const effective = cloneDocument(base);
  for (const [path, value] of booleanLeaves(ceiling)) {
    if (value === false) setNested(effective, path, false);
  }
  for (const path of SKA_PERMISSION_INTRODUCTION_V1_LEAVES) {
    if (getNested(ceiling, path).value !== true) {
      setNested(effective, path, false);
    }
  }
  return effective as FlagsMap;
}

/**
 * Convert a desired board-effective document to the minimal ceiling.
 *
 * A null ceiling means no restriction. Once a ceiling exists, introduced
 * leaves that stay enabled must be explicitly admitted with True because
 * Core treats an absent introduced ceiling leaf as denied.
 */
export function boardCeilingDelta(
  base: PermissionDocument,
  desired: PermissionDocument,
): PermissionDocument | null {
  const restrictions: string[] = [];
  for (const [path, baseValue] of booleanLeaves(base)) {
    const desiredValue = getNested(desired, path);
    if (baseValue === true && desiredValue.value !== true) {
      restrictions.push(path);
    }
  }
  if (restrictions.length === 0) return null;

  const ceiling: PermissionDocument = {};
  for (const path of restrictions) setNested(ceiling, path, false);
  for (const path of SKA_PERMISSION_INTRODUCTION_V1_LEAVES) {
    const baseValue = getNested(base, path).value;
    const desiredValue = getNested(desired, path).value;
    if (baseValue === true && desiredValue === true) {
      setNested(ceiling, path, true);
    }
  }
  return ceiling;
}

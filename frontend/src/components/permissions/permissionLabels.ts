/** Friendly labels for canonical top-level permission entities. */
export const ENTITY_LABELS = {
  board: 'Board',
  agent: 'Agents',
  permission_preset: 'Permission Presets',
  default_board_config: 'Default Board Configuration',
  design_system: 'Design Systems',
  runtime: 'Runtime',
  metrics: 'Metrics',
  amendment: 'Amendments',
  story: 'Stories',
  topic: 'Topics',
  spec: 'Specs',
  test_scenario: 'Test Scenarios',
  card: 'Cards',
  ideation: 'Ideations',
  refinement: 'Refinements',
  sprint: 'Sprints',
  profile: 'Profile',
  guidelines: 'Guidelines',
  kg: 'Knowledge Graphs',
  code_traceability: 'Code Traceability',
} as const;

export type PermissionEntity = keyof typeof ENTITY_LABELS;

/**
 * Text colors used by permission section labels.
 *
 * Keep these as complete, static Tailwind tokens: the type prevents a new
 * canonical entity from silently falling back to gray, while static strings
 * ensure every light/dark class is included in the production stylesheet.
 */
export const ENTITY_COLORS = {
  board: 'text-blue-600 dark:text-blue-400',
  agent: 'text-emerald-600 dark:text-emerald-300',
  permission_preset: 'text-purple-600 dark:text-purple-300',
  default_board_config: 'text-indigo-600 dark:text-indigo-300',
  design_system: 'text-fuchsia-600 dark:text-fuchsia-300',
  runtime: 'text-orange-600 dark:text-orange-300',
  metrics: 'text-lime-700 dark:text-lime-300',
  amendment: 'text-rose-600 dark:text-rose-300',
  story: 'text-sky-600 dark:text-sky-300',
  topic: 'text-teal-600 dark:text-teal-300',
  spec: 'text-violet-600 dark:text-violet-400',
  test_scenario: 'text-yellow-700 dark:text-yellow-300',
  card: 'text-green-600 dark:text-green-400',
  ideation: 'text-amber-600 dark:text-amber-400',
  refinement: 'text-cyan-600 dark:text-cyan-400',
  sprint: 'text-orange-600 dark:text-orange-400',
  profile: 'text-gray-600 dark:text-gray-400',
  guidelines: 'text-pink-600 dark:text-pink-400',
  kg: 'text-indigo-600 dark:text-indigo-400',
  code_traceability: 'text-cyan-700 dark:text-cyan-300',
} as const satisfies Record<PermissionEntity, string>;

/** Background and text colors used by permission summary chips. */
export const ENTITY_CHIP_COLORS = {
  board: 'bg-blue-50 text-blue-600 dark:bg-blue-900/20 dark:text-blue-300',
  agent: 'bg-emerald-50 text-emerald-600 dark:bg-emerald-900/20 dark:text-emerald-300',
  permission_preset: 'bg-purple-50 text-purple-600 dark:bg-purple-900/20 dark:text-purple-300',
  default_board_config: 'bg-indigo-50 text-indigo-600 dark:bg-indigo-900/20 dark:text-indigo-300',
  design_system: 'bg-fuchsia-50 text-fuchsia-600 dark:bg-fuchsia-900/20 dark:text-fuchsia-300',
  runtime: 'bg-orange-50 text-orange-600 dark:bg-orange-900/20 dark:text-orange-300',
  metrics: 'bg-lime-50 text-lime-700 dark:bg-lime-900/20 dark:text-lime-300',
  amendment: 'bg-rose-50 text-rose-600 dark:bg-rose-900/20 dark:text-rose-300',
  story: 'bg-sky-50 text-sky-600 dark:bg-sky-900/20 dark:text-sky-300',
  topic: 'bg-teal-50 text-teal-600 dark:bg-teal-900/20 dark:text-teal-300',
  spec: 'bg-violet-50 text-violet-600 dark:bg-violet-900/20 dark:text-violet-300',
  test_scenario: 'bg-yellow-50 text-yellow-700 dark:bg-yellow-900/20 dark:text-yellow-300',
  card: 'bg-green-50 text-green-600 dark:bg-green-900/20 dark:text-green-300',
  ideation: 'bg-amber-50 text-amber-600 dark:bg-amber-900/20 dark:text-amber-300',
  refinement: 'bg-cyan-50 text-cyan-600 dark:bg-cyan-900/20 dark:text-cyan-300',
  sprint: 'bg-orange-50 text-orange-600 dark:bg-orange-900/20 dark:text-orange-300',
  profile: 'bg-gray-50 text-gray-600 dark:bg-gray-700/50 dark:text-gray-400',
  guidelines: 'bg-pink-50 text-pink-600 dark:bg-pink-900/20 dark:text-pink-300',
  kg: 'bg-indigo-50 text-indigo-600 dark:bg-indigo-900/20 dark:text-indigo-300',
  code_traceability: 'bg-cyan-50 text-cyan-700 dark:bg-cyan-900/20 dark:text-cyan-300',
} as const satisfies Record<PermissionEntity, string>;

export const DEFAULT_ENTITY_COLOR = 'text-gray-600 dark:text-gray-400';
export const DEFAULT_ENTITY_CHIP_COLOR = 'bg-gray-50 text-gray-600 dark:bg-gray-700/50 dark:text-gray-400';

/** Human-readable presentation for canonical permission entity identifiers. */
export function getEntityLabel(entity: string): string {
  return ENTITY_LABELS[entity as PermissionEntity] ?? entity
    .split('_')
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

/** Light/dark-safe text classes for a canonical or future permission entity. */
export function getEntityTextClasses(entity: string): string {
  return ENTITY_COLORS[entity as PermissionEntity] ?? DEFAULT_ENTITY_COLOR;
}

/** Light/dark-safe chip classes for a canonical or future permission entity. */
export function getEntityChipClasses(entity: string): string {
  return ENTITY_CHIP_COLORS[entity as PermissionEntity] ?? DEFAULT_ENTITY_CHIP_COLOR;
}

import {
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  useEffect,
  useRef,
  useState,
} from 'react';
import { HorizontalOverflowNav } from './HorizontalOverflowNav';

export type AccessibleTabMountStrategy = 'active' | 'lazy-keep' | 'always';

export interface AccessibleTabItem<TId extends string> {
  id: TId;
  label: ReactNode;
  icon?: ReactNode;
  count?: number;
  attention?: boolean;
  disabled?: boolean;
  onActivate?: () => void;
}

export interface AccessibleTabListProps<TId extends string> {
  idBase: string;
  ariaLabel: string;
  items: readonly AccessibleTabItem<TId>[];
  value: TId;
  onValueChange: (id: TId) => void;
  orientation?: 'horizontal' | 'vertical';
  variant?: 'primary' | 'secondary';
  className?: string;
}

export interface AccessibleTabPanelProps<TId extends string> {
  idBase: string;
  tabId: TId;
  value: TId;
  mount?: AccessibleTabMountStrategy;
  className?: string;
  children: ReactNode;
}

function domToken(value: string): string {
  return value.replace(/[^A-Za-z0-9_-]/g, '-');
}

function accessibleTabId(idBase: string, tabId: string): string {
  return `${domToken(idBase)}-${domToken(tabId)}-tab`;
}

function accessibleTabPanelId(idBase: string, tabId: string): string {
  return `${domToken(idBase)}-${domToken(tabId)}-panel`;
}

const PRIMARY_SELECTED =
  'border-blue-500 text-blue-600 dark:text-blue-400';
const PRIMARY_IDLE =
  'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300';
const SECONDARY_SELECTED =
  'bg-white text-blue-700 shadow-sm dark:bg-surface-700 dark:text-blue-200';
const SECONDARY_IDLE =
  'text-surface-500 hover:text-surface-800 dark:text-surface-400 dark:hover:text-surface-100';

/**
 * Accessible, controlled tab list with manual activation.
 *
 * Arrow keys and Home/End move focus only. Enter, Space, or a pointer click
 * activates the focused tab. Keeping focus separate from selection prevents a
 * keyboard user from triggering expensive panels while exploring the list.
 */
export function AccessibleTabList<TId extends string>({
  idBase,
  ariaLabel,
  items,
  value,
  onValueChange,
  orientation = 'horizontal',
  variant = 'primary',
  className = '',
}: AccessibleTabListProps<TId>) {
  const enabledItems = items.filter((item) => !item.disabled);
  const selectedEnabledId = enabledItems.find((item) => item.id === value)?.id;
  const [focusedId, setFocusedId] = useState<TId | null>(
    selectedEnabledId ?? enabledItems[0]?.id ?? null,
  );
  const tabRefs = useRef(new Map<TId, HTMLButtonElement>());

  useEffect(() => {
    if (focusedId && enabledItems.some((item) => item.id === focusedId)) {
      return;
    }
    setFocusedId(selectedEnabledId ?? enabledItems[0]?.id ?? null);
  }, [enabledItems, focusedId, selectedEnabledId]);

  useEffect(() => {
    if (selectedEnabledId) {
      setFocusedId(selectedEnabledId);
    }
  }, [selectedEnabledId]);

  const activate = (item: AccessibleTabItem<TId>) => {
    if (item.disabled) return;
    item.onActivate?.();
    onValueChange(item.id);
  };

  const focusItem = (item: AccessibleTabItem<TId>) => {
    setFocusedId(item.id);
    const element = tabRefs.current.get(item.id);
    element?.focus();
    if (typeof element?.scrollIntoView === 'function') {
      element.scrollIntoView({
        behavior: 'smooth',
        block: 'nearest',
        inline: 'nearest',
      });
    }
  };

  const handleKeyDown = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    item: AccessibleTabItem<TId>,
  ) => {
    if (item.disabled) return;

    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      activate(item);
      return;
    }

    const currentIndex = enabledItems.findIndex(
      (candidate) => candidate.id === item.id,
    );
    if (currentIndex < 0 || enabledItems.length === 0) return;

    let nextIndex: number | null = null;
    if (
      (orientation === 'horizontal' && event.key === 'ArrowRight')
      || (orientation === 'vertical' && event.key === 'ArrowDown')
    ) {
      nextIndex = (currentIndex + 1) % enabledItems.length;
    } else if (
      (orientation === 'horizontal' && event.key === 'ArrowLeft')
      || (orientation === 'vertical' && event.key === 'ArrowUp')
    ) {
      nextIndex =
        (currentIndex - 1 + enabledItems.length) % enabledItems.length;
    } else if (event.key === 'Home') {
      nextIndex = 0;
    } else if (event.key === 'End') {
      nextIndex = enabledItems.length - 1;
    }

    if (nextIndex === null) return;
    event.preventDefault();
    focusItem(enabledItems[nextIndex]);
  };

  const listTone = variant === 'secondary'
    ? 'inline-flex max-w-full rounded-lg border border-surface-200 bg-surface-50 p-1 dark:border-surface-700 dark:bg-surface-900'
    : 'flex border-b border-gray-200 dark:border-gray-700';
  const orientationClass = orientation === 'vertical'
    ? 'flex-col overflow-y-auto'
    : 'items-center gap-1 overflow-x-auto pb-1';

  const tabButtons = items.map((item) => {
        const selected = item.id === value;
        const primaryTone = selected ? PRIMARY_SELECTED : PRIMARY_IDLE;
        const secondaryTone = selected ? SECONDARY_SELECTED : SECONDARY_IDLE;
        const tone = variant === 'secondary' ? secondaryTone : primaryTone;
        const shape = variant === 'secondary'
          ? 'rounded-md px-3 py-1.5 text-xs'
          : 'shrink-0 whitespace-nowrap border-b-2 px-3 py-2 text-sm -mb-px';

        return (
          <button
            key={item.id}
            ref={(element) => {
              if (element) tabRefs.current.set(item.id, element);
              else tabRefs.current.delete(item.id);
            }}
            id={accessibleTabId(idBase, item.id)}
            type="button"
            role="tab"
            aria-selected={selected}
            aria-controls={accessibleTabPanelId(idBase, item.id)}
            tabIndex={focusedId === item.id ? 0 : -1}
            disabled={item.disabled}
            onFocus={() => {
              if (!item.disabled) setFocusedId(item.id);
            }}
            onClick={() => activate(item)}
            onKeyDown={(event) => handleKeyDown(event, item)}
            className={`flex items-center gap-1.5 font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${shape} ${tone}`}
          >
            {item.icon}
            <span>{item.label}</span>
            {item.count !== undefined && item.count > 0 && (
              <>
                {' '}
                <span
                  className={`rounded-full px-1.5 py-0.5 text-[10px] ${
                    item.attention
                      ? 'bg-amber-200 text-amber-700 dark:bg-amber-800 dark:text-amber-300'
                      : 'bg-gray-200 text-gray-600 dark:bg-gray-600 dark:text-gray-300'
                  }`}
                >
                  {item.count}
                </span>
              </>
            )}
          </button>
        );
      });

  const listClassName =
    `${listTone} ${orientationClass} ${className}`.trim();

  if (orientation === 'vertical') {
    return (
      <div
        role="tablist"
        aria-label={ariaLabel}
        aria-orientation={orientation}
        className={listClassName}
      >
        {tabButtons}
      </div>
    );
  }

  return (
    <HorizontalOverflowNav
      role="tablist"
      aria-label={ariaLabel}
      aria-orientation={orientation}
      controlsLabel={ariaLabel}
      className={listClassName}
    >
      {tabButtons}
    </HorizontalOverflowNav>
  );
}

/**
 * Panel companion for AccessibleTabList.
 *
 * `lazy-keep` mounts on first activation and then hides instead of unmounting,
 * preserving drafts and expensive resource workspaces while users change tabs.
 */
export function AccessibleTabPanel<TId extends string>({
  idBase,
  tabId,
  value,
  mount = 'active',
  className = '',
  children,
}: AccessibleTabPanelProps<TId>) {
  const selected = value === tabId;
  const [hasBeenActive, setHasBeenActive] = useState(selected);

  useEffect(() => {
    if (selected) setHasBeenActive(true);
  }, [selected]);

  if (mount === 'active' && !selected) return null;
  if (mount === 'lazy-keep' && !selected && !hasBeenActive) return null;

  return (
    <section
      id={accessibleTabPanelId(idBase, tabId)}
      role="tabpanel"
      aria-labelledby={accessibleTabId(idBase, tabId)}
      hidden={!selected}
      tabIndex={0}
      className={className}
    >
      {children}
    </section>
  );
}

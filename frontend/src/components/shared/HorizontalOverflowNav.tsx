import {
  type HTMLAttributes,
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';

interface OverflowState {
  left: boolean;
  right: boolean;
}

interface HorizontalOverflowNavProps
  extends Omit<HTMLAttributes<HTMLDivElement>, 'children'> {
  children: ReactNode;
  controlsLabel: string;
}

const EDGE_EPSILON = 2;
const CONTROL_INSET = 42;

/**
 * Adds discoverable navigation to a horizontally scrollable row.
 *
 * The viewport remains the native scroll container, so mouse wheels,
 * trackpads, touch gestures, and browser scrollbars keep their normal
 * behaviour. Arrow buttons are siblings of the viewport and therefore never
 * pollute tablist semantics.
 */
export function HorizontalOverflowNav({
  children,
  controlsLabel,
  className = '',
  ...viewportProps
}: HorizontalOverflowNavProps) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const [overflow, setOverflow] = useState<OverflowState>({
    left: false,
    right: false,
  });

  const updateOverflow = useCallback(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;

    const next = {
      left: viewport.scrollLeft > EDGE_EPSILON,
      right:
        viewport.scrollLeft + viewport.clientWidth
        < viewport.scrollWidth - EDGE_EPSILON,
    };
    setOverflow((current) => (
      current.left === next.left && current.right === next.right
        ? current
        : next
    ));
  }, []);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return undefined;

    updateOverflow();
    viewport.addEventListener('scroll', updateOverflow, { passive: true });
    window.addEventListener('resize', updateOverflow);

    const resizeObserver = typeof ResizeObserver === 'undefined'
      ? null
      : new ResizeObserver(updateOverflow);
    resizeObserver?.observe(viewport);
    Array.from(viewport.children).forEach((child) => {
      if (child instanceof HTMLElement) resizeObserver?.observe(child);
    });

    let active = true;
    void document.fonts?.ready.then(() => {
      if (active) updateOverflow();
    });

    return () => {
      active = false;
      viewport.removeEventListener('scroll', updateOverflow);
      window.removeEventListener('resize', updateOverflow);
      resizeObserver?.disconnect();
    };
  }, [children, updateOverflow]);

  const scrollToHiddenItem = (direction: 'left' | 'right') => {
    const viewport = viewportRef.current;
    if (!viewport) return;

    const items = Array.from(viewport.children).filter(
      (child): child is HTMLElement => child instanceof HTMLElement,
    );
    const visibleLeft = viewport.scrollLeft + CONTROL_INSET;
    const visibleRight =
      viewport.scrollLeft + viewport.clientWidth - CONTROL_INSET;

    let target: HTMLElement | undefined;
    if (direction === 'right') {
      target = items.find(
        (item) => item.offsetLeft + item.offsetWidth > visibleRight + EDGE_EPSILON,
      );
    } else {
      for (let index = items.length - 1; index >= 0; index -= 1) {
        if (items[index].offsetLeft < visibleLeft - EDGE_EPSILON) {
          target = items[index];
          break;
        }
      }
    }

    const maxScroll = Math.max(0, viewport.scrollWidth - viewport.clientWidth);
    const desiredLeft = target
      ? direction === 'right'
        ? target.offsetLeft + target.offsetWidth - viewport.clientWidth + CONTROL_INSET
        : target.offsetLeft - CONTROL_INSET
      : viewport.scrollLeft
        + (direction === 'right' ? 1 : -1) * viewport.clientWidth * 0.75;
    const left = Math.min(maxScroll, Math.max(0, desiredLeft));

    const reduceMotion = window.matchMedia?.(
      '(prefers-reduced-motion: reduce)',
    ).matches;
    if (typeof viewport.scrollTo === 'function') {
      viewport.scrollTo({
        left,
        behavior: reduceMotion ? 'auto' : 'smooth',
      });
    } else {
      viewport.scrollLeft = left;
      updateOverflow();
    }
  };

  const controlClass =
    'pointer-events-auto inline-flex h-8 w-8 items-center justify-center rounded-full '
    + 'border border-surface-200/90 bg-white/90 text-surface-600 shadow-md backdrop-blur-md '
    + 'transition hover:border-accent-300 hover:text-accent-600 focus:outline-none '
    + 'focus-visible:ring-2 focus-visible:ring-accent-500 focus-visible:ring-offset-2 '
    + 'dark:border-white/15 dark:bg-black/55 dark:text-surface-100 '
    + 'dark:hover:border-accent-500 dark:hover:text-accent-300 dark:focus-visible:ring-offset-surface-900';

  return (
    <div className="relative min-w-0 max-w-full">
      <div
        {...viewportProps}
        ref={viewportRef}
        className={className}
      >
        {children}
      </div>

      {overflow.left && (
        <div className="pointer-events-none absolute inset-y-0 left-0 z-10 flex items-center pl-1">
          <button
            type="button"
            aria-label={`Scroll ${controlsLabel} left`}
            title="Show previous hidden tab"
            onClick={() => scrollToHiddenItem('left')}
            className={controlClass}
          >
            <ChevronLeft size={18} aria-hidden="true" />
          </button>
        </div>
      )}

      {overflow.right && (
        <div className="pointer-events-none absolute inset-y-0 right-0 z-10 flex items-center pr-1">
          <button
            type="button"
            aria-label={`Scroll ${controlsLabel} right`}
            title="Show next hidden tab"
            onClick={() => scrollToHiddenItem('right')}
            className={controlClass}
          >
            <ChevronRight size={18} aria-hidden="true" />
          </button>
        </div>
      )}
    </div>
  );
}

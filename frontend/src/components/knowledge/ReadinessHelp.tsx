import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { CircleHelp } from "lucide-react";

/** A fixed portal: help never changes the panel's measured size or scroll area. */
export function ReadinessHelp({
  label,
  children,
}: {
  label: string;
  children: string;
}) {
  const id = useId();
  const anchor = useRef<HTMLButtonElement>(null);
  const [position, setPosition] = useState<{
    left: number;
    top?: number;
    bottom?: number;
  } | null>(null);
  const show = () => {
    const box = anchor.current?.getBoundingClientRect();
    if (!box) return;
    const left = Math.max(8, Math.min(box.left, window.innerWidth - 336));
    setPosition(
      box.bottom + 150 > window.innerHeight
        ? { left, bottom: window.innerHeight - box.top + 8 }
        : { left, top: box.bottom + 8 },
    );
  };
  useEffect(() => {
    if (!position) return;
    const close = () => setPosition(null);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", close, true);
    return () => {
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [position]);
  return (
    <span className="inline-flex align-middle ml-1">
      <button
        ref={anchor}
        type="button"
        aria-label={`Help: ${label}`}
        aria-describedby={position ? id : undefined}
        onMouseEnter={show}
        onMouseLeave={() => setPosition(null)}
        onFocus={show}
        onBlur={() => setPosition(null)}
        onClick={show}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.stopPropagation();
            setPosition(null);
          }
        }}
        className="rounded text-violet-500 focus-visible:outline focus-visible:outline-2"
      >
        <CircleHelp size={16} />
      </button>
      {position &&
        createPortal(
          <span
            id={id}
            role="tooltip"
            style={position}
            className="fixed z-[100] pointer-events-none w-80 max-w-[calc(100vw-16px)] rounded-lg bg-slate-950 text-white border border-slate-600 p-3 shadow-xl text-sm"
          >
            {children}
          </span>,
          document.body,
        )}
    </span>
  );
}

import { ChevronRight } from 'lucide-react';

interface BreadcrumbSegment {
  label: string;
  onClick?: () => void;
}

interface BreadcrumbProps {
  segments: BreadcrumbSegment[];
}

export function Breadcrumb({ segments }: BreadcrumbProps) {
  return (
    <nav className="flex items-center gap-1 text-sm">
      {segments.map((segment, index) => {
        const isLast = index === segments.length - 1;
        return (
          <span key={index} className="flex items-center gap-1">
            {index > 0 && (
              <ChevronRight className="w-4 h-4 text-gray-400 dark:text-gray-500" />
            )}
            {segment.onClick && !isLast ? (
              <button
                onClick={segment.onClick}
                className="text-blue-600 dark:text-blue-400 hover:underline"
              >
                {segment.label}
              </button>
            ) : (
              <span className="text-gray-900 dark:text-white font-medium">
                {segment.label}
              </span>
            )}
          </span>
        );
      })}
    </nav>
  );
}

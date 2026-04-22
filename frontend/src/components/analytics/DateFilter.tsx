import { useState } from 'react';

type Preset = '7d' | '30d' | '90d' | 'custom';

interface DateFilterProps {
  from: string;
  to: string;
  onChange: (from: string, to: string) => void;
}

function daysAgo(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().split('T')[0];
}

function today(): string {
  return new Date().toISOString().split('T')[0];
}

export function DateFilter({ from, to, onChange }: DateFilterProps) {
  const [activePreset, setActivePreset] = useState<Preset>('30d');

  const applyPreset = (preset: Preset) => {
    setActivePreset(preset);
    if (preset === '7d') onChange(daysAgo(7), today());
    else if (preset === '30d') onChange(daysAgo(30), today());
    else if (preset === '90d') onChange(daysAgo(90), today());
  };

  const handleFromChange = (value: string) => {
    setActivePreset('custom');
    onChange(value, to);
  };

  const handleToChange = (value: string) => {
    setActivePreset('custom');
    onChange(from, value);
  };

  const presets: { id: Preset; label: string }[] = [
    { id: '7d', label: '7d' },
    { id: '30d', label: '30d' },
    { id: '90d', label: '90d' },
    { id: 'custom', label: 'Custom' },
  ];

  return (
    <div className="flex items-center gap-2">
      <div className="flex items-center gap-0.5 bg-gray-200 dark:bg-gray-700 rounded-md p-0.5">
        {presets.map((preset) => (
          <button
            key={preset.id}
            onClick={() => {
              if (preset.id !== 'custom') applyPreset(preset.id);
              else setActivePreset('custom');
            }}
            className={`px-2 py-1 rounded text-xs font-medium transition-colors ${
              activePreset === preset.id
                ? 'bg-white dark:bg-gray-800 text-gray-900 dark:text-white shadow-sm'
                : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white'
            }`}
          >
            {preset.label}
          </button>
        ))}
      </div>

      <input
        type="date"
        value={from}
        onChange={(e) => handleFromChange(e.target.value)}
        className="px-2 py-1 text-xs rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-white"
      />
      <span className="text-gray-400 text-xs">to</span>
      <input
        type="date"
        value={to}
        onChange={(e) => handleToChange(e.target.value)}
        className="px-2 py-1 text-xs rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-white"
      />
    </div>
  );
}

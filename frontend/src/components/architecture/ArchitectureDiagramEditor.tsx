import { useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react';
import {
  Boxes,
  Code2,
  Cloud,
  Cpu,
  Database,
  Focus,
  Fullscreen,
  Grid3X3,
  Globe2,
  HardDrive,
  Lock,
  MessageSquare,
  Minimize2,
  Monitor,
  MousePointer2,
  Network,
  Package,
  RotateCcw,
  Server,
  Smartphone,
  Terminal,
  Trash2,
  Type,
  UserRound,
  Workflow,
  ZoomIn,
  ZoomOut,
  type LucideIcon,
} from 'lucide-react';
import type {
  ArchitectureDiagram,
  ArchitectureEntity,
  ArchitectureInterface,
  ScreenMockup,
} from '@/types';

type DiagramMode = 'visual' | 'raw';
type ConnectionType = 'direct' | 'elbow';
type InterfaceDirection = 'source_to_target' | 'target_to_source' | 'bidirectional' | 'none';
type ConnectionAnchor = 'top' | 'right' | 'bottom' | 'left';

interface ExcalidrawElement {
  id: string;
  type: string;
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  text?: string;
  strokeColor?: string;
  backgroundColor?: string;
  points?: number[][];
  architectureKind?: string | null;
  displayType?: string | null;
  iconName?: string | null;
  linkedEntityId?: string | null;
  linkedInterfaceId?: string | null;
  linkedInterfaceIds?: string[] | null;
  linkedMockupId?: string | null;
  sourceElementId?: string | null;
  targetElementId?: string | null;
  sourceAnchor?: ConnectionAnchor | null;
  targetAnchor?: ConnectionAnchor | null;
  lineStyle?: 'solid' | 'dashed';
  connectionType?: ConnectionType | null;
}

interface DragState {
  id: string;
  startClientX: number;
  startClientY: number;
  startScrollLeft: number;
  startScrollTop: number;
  startX: number;
  startY: number;
}

interface PanState {
  startClientX: number;
  startClientY: number;
  startScrollLeft: number;
  startScrollTop: number;
}

interface ResizeState {
  id: string;
  startClientX: number;
  startClientY: number;
  startScrollLeft: number;
  startScrollTop: number;
  startWidth: number;
  startHeight: number;
}

interface ElementBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

interface ArchitectureDiagramEditorProps {
  diagram: ArchitectureDiagram | null;
  entities?: ArchitectureEntity[];
  interfaces?: ArchitectureInterface[];
  mockups?: ScreenMockup[];
  readOnly?: boolean;
  onChange: (diagram: ArchitectureDiagram) => void;
  onDeleteLinkedEntity?: (entityRef: string) => void;
}

const BASE_CANVAS_WIDTH = 1600;
const BASE_CANVAS_HEIGHT = 1000;
const CANVAS_MARGIN = 420;
const GRID_SIZE = 24;
const MIN_ZOOM = 0.5;
const MAX_ZOOM = 2;
const ZOOM_STEP = 0.1;
const MIN_NODE_WIDTH = 96;
const MIN_NODE_HEIGHT = 48;

const inputClass = 'mt-1 w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-700 rounded bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100';
const labelClass = 'text-xs font-medium text-gray-500 dark:text-gray-400';
const iconButtonClass = 'p-1.5 rounded text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-50';

function asPayload(diagram: ArchitectureDiagram | null): Record<string, unknown> {
  if (!diagram || typeof diagram.adapter_payload !== 'object' || Array.isArray(diagram.adapter_payload) || diagram.adapter_payload === null) {
    return { type: 'excalidraw', version: 2, elements: [], appState: {}, files: {} };
  }
  return diagram.adapter_payload as Record<string, unknown>;
}

function getElements(diagram: ArchitectureDiagram | null): ExcalidrawElement[] {
  const payload = asPayload(diagram);
  return Array.isArray(payload.elements) ? (payload.elements as ExcalidrawElement[]) : [];
}

function nextElementId(prefix: string): string {
  return `${prefix}_${Date.now()}_${Math.random().toString(16).slice(2, 7)}`;
}

function itemRef(item: { id?: string | null; name?: string; title?: string }, index: number, prefix: string): string {
  return item.id || item.name || item.title || `${prefix}_${index}`;
}

function refKey(value: string | null | undefined): string {
  return (value || '').trim().toLowerCase();
}

function uniqueRefs(values: Array<string | null | undefined>): string[] {
  const refs: string[] = [];
  values.forEach((value) => {
    const ref = value?.trim();
    if (ref && !refs.includes(ref)) refs.push(ref);
  });
  return refs;
}

function itemRefs(item: { id?: string | null; name?: string; title?: string }, index: number, prefix: string): string[] {
  return uniqueRefs([item.id || undefined, item.name || undefined, item.title || undefined, `${prefix}_${index}`]);
}

function linkedInterfaceRefs(element: ExcalidrawElement): string[] {
  return uniqueRefs([
    element.linkedInterfaceId || undefined,
    ...(Array.isArray(element.linkedInterfaceIds) ? element.linkedInterfaceIds : []),
  ]);
}

function withLinkedInterfaceRefs(element: ExcalidrawElement, refs: string[]): ExcalidrawElement {
  const unique = uniqueRefs(refs);
  return {
    ...element,
    linkedInterfaceId: unique[0] || null,
    linkedInterfaceIds: unique,
  };
}

function titleCase(value: string): string {
  return value
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\w\S*/g, (part) => part.charAt(0).toUpperCase() + part.slice(1).toLowerCase());
}

function isDefaultLightFill(value: string | undefined): boolean {
  if (!value) return true;
  return ['#ecfeff', '#e0f2fe', '#ffffff', '#fff', 'white', 'transparent'].includes(value.toLowerCase());
}

function elementTypeLabel(element: ExcalidrawElement): string {
  if (element.displayType?.trim()) return element.displayType;
  if (element.architectureKind?.trim()) return titleCase(element.architectureKind);
  if (element.type === 'arrow') return 'Edge';
  if (element.type === 'text') return 'Note';
  return 'Element';
}

function elementName(element: ExcalidrawElement, fallback = 'Unnamed'): string {
  if (element.text?.trim()) return element.text;
  if (element.type === 'arrow') return fallback === 'Unnamed' ? 'Connection' : fallback;
  if (element.type === 'text') return 'Note';
  return fallback;
}

function elementBox(element: ExcalidrawElement): ElementBox {
  return {
    x: Number(element.x ?? 40),
    y: Number(element.y ?? 40),
    width: Math.max(40, Number(element.width ?? 140)),
    height: Math.max(element.type === 'arrow' ? 2 : 32, Number(element.height ?? 80)),
  };
}

function elementCenter(element: ExcalidrawElement): { x: number; y: number } {
  const box = elementBox(element);
  return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
}

function anchorPoint(box: ElementBox, anchor: ConnectionAnchor): { x: number; y: number } {
  if (anchor === 'top') return { x: box.x + box.width / 2, y: box.y };
  if (anchor === 'right') return { x: box.x + box.width, y: box.y + box.height / 2 };
  if (anchor === 'bottom') return { x: box.x + box.width / 2, y: box.y + box.height };
  return { x: box.x, y: box.y + box.height / 2 };
}

function bestAnchorsBetween(source: ExcalidrawElement, target: ExcalidrawElement): { sourceAnchor: ConnectionAnchor; targetAnchor: ConnectionAnchor } {
  const sourceCenter = elementCenter(source);
  const targetCenter = elementCenter(target);
  const dx = targetCenter.x - sourceCenter.x;
  const dy = targetCenter.y - sourceCenter.y;
  if (Math.abs(dx) >= Math.abs(dy)) {
    return dx >= 0
      ? { sourceAnchor: 'right', targetAnchor: 'left' }
      : { sourceAnchor: 'left', targetAnchor: 'right' };
  }
  return dy >= 0
    ? { sourceAnchor: 'bottom', targetAnchor: 'top' }
    : { sourceAnchor: 'top', targetAnchor: 'bottom' };
}

function clampZoom(value: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Number(value.toFixed(2))));
}

function snapToGrid(value: number): number {
  return Math.max(0, Math.round(value / GRID_SIZE) * GRID_SIZE);
}

function snapSizeToGrid(value: number, minimum: number): number {
  return Math.max(minimum, Math.round(value / GRID_SIZE) * GRID_SIZE);
}

function normalizeConnectionType(value: string | null | undefined): ConnectionType {
  return value === 'elbow' ? 'elbow' : 'direct';
}

function normalizeInterfaceDirection(value: string | null | undefined): InterfaceDirection {
  if (value === 'target_to_source' || value === 'bidirectional' || value === 'none') return value;
  return 'source_to_target';
}

function interfaceDirectionLabel(value: string | null | undefined): string {
  const normalized = normalizeInterfaceDirection(value);
  if (normalized === 'target_to_source') return 'Target -> Source';
  if (normalized === 'bidirectional') return 'Bidirectional';
  if (normalized === 'none') return 'No arrow';
  return 'Source -> Target';
}

function combinedInterfaceDirection(values: InterfaceDirection[]): InterfaceDirection {
  const directions = values.filter((value) => value !== 'none');
  if (directions.length === 0) return 'none';
  if (directions.includes('bidirectional')) return 'bidirectional';
  if (directions.includes('source_to_target') && directions.includes('target_to_source')) return 'bidirectional';
  return directions.includes('target_to_source') ? 'target_to_source' : 'source_to_target';
}

function pathMiddlePoint(points: Array<{ x: number; y: number }>): { x: number; y: number } {
  if (points.length === 0) return { x: 0, y: 0 };
  if (points.length === 1) return points[0];
  const lengths = points.slice(1).map((point, index) => {
    const previous = points[index];
    return Math.hypot(point.x - previous.x, point.y - previous.y);
  });
  const total = lengths.reduce((sum, length) => sum + length, 0);
  if (total <= 0) return points[0];
  let walked = 0;
  const halfway = total / 2;
  for (let index = 1; index < points.length; index += 1) {
    const segmentLength = lengths[index - 1];
    if (walked + segmentLength >= halfway) {
      const previous = points[index - 1];
      const current = points[index];
      const ratio = segmentLength <= 0 ? 0 : (halfway - walked) / segmentLength;
      return {
        x: previous.x + (current.x - previous.x) * ratio,
        y: previous.y + (current.y - previous.y) * ratio,
      };
    }
    walked += segmentLength;
  }
  return points[points.length - 1];
}

function iconForElement(element: ExcalidrawElement): LucideIcon {
  if (element.iconName === 'server') return Server;
  if (element.iconName === 'database') return Database;
  if (element.iconName === 'message') return MessageSquare;
  if (element.iconName === 'user') return UserRound;
  if (element.iconName === 'network') return Network;
  if (element.iconName === 'boxes') return Boxes;
  if (element.iconName === 'cloud') return Cloud;
  if (element.iconName === 'cpu') return Cpu;
  if (element.iconName === 'globe') return Globe2;
  if (element.iconName === 'hard_drive') return HardDrive;
  if (element.iconName === 'lock') return Lock;
  if (element.iconName === 'monitor') return Monitor;
  if (element.iconName === 'package') return Package;
  if (element.iconName === 'smartphone') return Smartphone;
  if (element.iconName === 'terminal') return Terminal;
  if (element.iconName === 'workflow') return Workflow;
  const value = `${element.architectureKind || ''} ${element.displayType || ''}`.toLowerCase();
  if (value.includes('database') || value.includes('repository') || value.includes('store')) return Database;
  if (value.includes('api') || value.includes('server') || value.includes('runtime') || value.includes('node')) return Server;
  if (value.includes('queue') || value.includes('message') || value.includes('interface')) return MessageSquare;
  if (value.includes('actor') || value.includes('user') || value.includes('external_entity')) return UserRound;
  if (value.includes('network') || value.includes('adapter') || value.includes('flow') || value.includes('relationship')) return Network;
  if (element.type === 'text') return Type;
  return Boxes;
}

function getElementsBounds(elements: ExcalidrawElement[]): { minX: number; minY: number; maxX: number; maxY: number; width: number; height: number } | null {
  const visible = elements.filter((element) => element.type !== 'arrow' || (!element.sourceElementId && !element.targetElementId));
  if (visible.length === 0) return null;
  const boxes = visible.map(elementBox);
  const minX = Math.min(...boxes.map((box) => box.x));
  const minY = Math.min(...boxes.map((box) => box.y));
  const maxX = Math.max(...boxes.map((box) => box.x + box.width));
  const maxY = Math.max(...boxes.map((box) => box.y + box.height));
  return { minX, minY, maxX, maxY, width: maxX - minX, height: maxY - minY };
}

function detailText(value: unknown): string {
  if (value === null || value === undefined || value === '') return '';
  if (Array.isArray(value)) return value.length > 0 ? value.join(', ') : '';
  if (typeof value === 'object') return JSON.stringify(value, null, 2);
  return String(value);
}

function DetailRow({ label, children }: { label: string; children?: ReactNode }) {
  const empty = children === null || children === undefined || children === '';
  return (
    <div className="grid grid-cols-[132px_minmax(0,1fr)] gap-3 border-b border-gray-200 dark:border-gray-800 py-2 last:border-b-0">
      <dt className="text-xs font-medium uppercase text-gray-500 dark:text-gray-400">{label}</dt>
      <dd className="min-w-0 text-sm text-gray-900 dark:text-gray-100">
        {empty ? <span className="text-gray-400 dark:text-gray-500">Not set</span> : children}
      </dd>
    </div>
  );
}

function DetailCode({ value }: { value: unknown }) {
  const text = detailText(value);
  if (!text) return <span className="text-gray-400 dark:text-gray-500">Not set</span>;
  return (
    <pre className="max-h-36 overflow-auto whitespace-pre-wrap rounded border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-950 p-2 text-xs text-gray-800 dark:text-gray-100 [scrollbar-gutter:stable]">
      {text}
    </pre>
  );
}

export function ArchitectureDiagramEditor({
  diagram,
  entities = [],
  interfaces = [],
  mockups = [],
  readOnly = false,
  onChange,
  onDeleteLinkedEntity,
}: ArchitectureDiagramEditorProps) {
  const [mode, setMode] = useState<DiagramMode>('visual');
  const [selectedElementId, setSelectedElementId] = useState<string>('');
  const [rawDraft, setRawDraft] = useState('');
  const [showGrid, setShowGrid] = useState(true);
  const [zoom, setZoom] = useState(1);
  const [connectMode, setConnectMode] = useState(false);
  const [connectSourceId, setConnectSourceId] = useState('');
  const [connectSourceAnchor, setConnectSourceAnchor] = useState<ConnectionAnchor | null>(null);
  const [isPanning, setIsPanning] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [detailsElementId, setDetailsElementId] = useState('');
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<DragState | null>(null);
  const resizeRef = useRef<ResizeState | null>(null);
  const panRef = useRef<PanState | null>(null);
  const payload = useMemo(() => asPayload(diagram), [diagram]);
  const elements = useMemo(() => getElements(diagram), [diagram]);
  const selectedElement = elements.find((item) => item.id === selectedElementId) || null;
  const nodeElements = elements.filter((element) => element.type !== 'arrow');
  const edgeElements = elements.filter((element) => element.type === 'arrow');

  const entityDetailsByRef = useMemo(() => {
    const map = new Map<string, { ref: string; label: string; item: ArchitectureEntity; index: number }>();
    entities.forEach((entity, index) => {
      itemRefs(entity, index, 'entity').forEach((ref) => {
        map.set(refKey(ref), {
          ref,
          label: entity.name || `Entity ${index + 1}`,
          item: entity,
          index,
        });
      });
    });
    return map;
  }, [entities]);

  const interfaceDetailsByRef = useMemo(() => {
    const map = new Map<string, { ref: string; label: string; item: ArchitectureInterface; index: number }>();
    interfaces.forEach((item, index) => {
      itemRefs(item, index, 'interface').forEach((ref) => {
        map.set(refKey(ref), {
          ref,
          label: item.name || `Interface ${index + 1}`,
          item,
          index,
        });
      });
    });
    return map;
  }, [interfaces]);

  const entityOptions = useMemo(
    () => entities.map((entity, index) => ({ id: itemRef(entity, index, 'entity'), label: entity.name || `Entity ${index + 1}` })),
    [entities],
  );
  const interfaceOptions = useMemo(
    () => interfaces.map((item, index) => ({
      id: itemRef(item, index, 'interface'),
      label: item.name || `Interface ${index + 1}`,
      endpoint: item.endpoint || '',
      direction: normalizeInterfaceDirection(item.direction),
      protocol: item.protocol || '',
    })),
    [interfaces],
  );
  const mockupOptions = useMemo(
    () => mockups.map((mockup, index) => ({ id: itemRef(mockup, index, 'mockup'), label: `${mockup.title || `Screen ${index + 1}`} (${mockup.screen_type})` })),
    [mockups],
  );

  const canvasSize = useMemo(() => {
    const bounds = getElementsBounds(elements);
    return {
      width: Math.max(BASE_CANVAS_WIDTH, (bounds?.maxX || 0) + CANVAS_MARGIN),
      height: Math.max(BASE_CANVAS_HEIGHT, (bounds?.maxY || 0) + CANVAS_MARGIN),
    };
  }, [elements]);

  useEffect(() => {
    setRawDraft(JSON.stringify(payload, null, 2));
  }, [payload]);

  useEffect(() => {
    if (elements.length === 0) {
      setSelectedElementId('');
      return;
    }
    if (selectedElementId && !elements.some((item) => item.id === selectedElementId)) {
      setSelectedElementId('');
    }
    if (detailsElementId && !elements.some((item) => item.id === detailsElementId)) {
      setDetailsElementId('');
    }
  }, [detailsElementId, elements, selectedElementId]);

  const updateElements = (nextElements: ExcalidrawElement[]) => {
    if (!diagram) return;
    onChange({
      ...diagram,
      format: 'excalidraw_json',
      adapter_payload: {
        ...payload,
        type: payload.type || 'excalidraw',
        version: payload.version || 2,
        appState: payload.appState || {},
        files: payload.files || {},
        elements: nextElements,
      },
    });
  };

  const deleteElementById = (elementId: string) => {
    if (readOnly) return;
    const target = elements.find((item) => item.id === elementId);
    if (target?.type !== 'arrow' && target?.linkedEntityId && onDeleteLinkedEntity) {
      onDeleteLinkedEntity(target.linkedEntityId);
      setSelectedElementId('');
      if (connectSourceId === elementId) setConnectSourceId('');
      if (connectSourceId === elementId) setConnectSourceAnchor(null);
      return;
    }
    const next = elements.filter((item) => item.id !== elementId && item.sourceElementId !== elementId && item.targetElementId !== elementId);
    updateElements(next);
    setSelectedElementId('');
    if (connectSourceId === elementId) setConnectSourceId('');
    if (connectSourceId === elementId) setConnectSourceAnchor(null);
  };

  useEffect(() => {
    if (mode !== 'visual' || readOnly || !selectedElementId) return undefined;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Delete' && event.key !== 'Backspace') return;
      const target = event.target as HTMLElement | null;
      if (target instanceof HTMLElement && target.closest('input, textarea, select, [contenteditable="true"]')) return;
      event.preventDefault();
      deleteElementById(selectedElementId);
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [connectSourceId, elements, mode, payload, readOnly, selectedElementId]);

  const updateSelected = (patch: Partial<ExcalidrawElement>) => {
    if (!selectedElement || readOnly) return;
    updateElements(elements.map((item) => (item.id === selectedElement.id ? { ...item, ...patch } : item)));
  };

  const openElementDetails = (event: ReactMouseEvent<HTMLButtonElement>, element: ExcalidrawElement) => {
    event.preventDefault();
    event.stopPropagation();
    setSelectedElementId(element.id);
    setDetailsElementId(element.id);
  };

  const applyRawPayload = () => {
    if (!diagram || readOnly) return;
    try {
      const parsed = JSON.parse(rawDraft);
      onChange({ ...diagram, adapter_payload: parsed, format: 'excalidraw_json' });
    } catch {
      window.alert('Invalid JSON payload');
    }
  };

  const autoScrollNearEdge = (event: ReactPointerEvent<HTMLElement>) => {
    const viewport = scrollRef.current;
    if (!viewport) return;
    const rect = viewport.getBoundingClientRect();
    const edge = 44;
    const step = GRID_SIZE;
    if (rect.width <= 0 || rect.height <= 0) return;
    if (event.clientX > rect.right - edge) viewport.scrollLeft += step;
    if (event.clientX < rect.left + edge) viewport.scrollLeft = Math.max(0, viewport.scrollLeft - step);
    if (event.clientY > rect.bottom - edge) viewport.scrollTop += step;
    if (event.clientY < rect.top + edge) viewport.scrollTop = Math.max(0, viewport.scrollTop - step);
  };

  const beginDrag = (event: ReactPointerEvent<HTMLButtonElement>, element: ExcalidrawElement) => {
    setSelectedElementId(element.id);
    if (readOnly || connectMode || (element.type === 'arrow' && element.sourceElementId && element.targetElementId)) return;
    event.preventDefault();
    event.stopPropagation();
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // Some test/browser environments do not expose pointer capture.
    }
    dragRef.current = {
      id: element.id,
      startClientX: event.clientX,
      startClientY: event.clientY,
      startScrollLeft: scrollRef.current?.scrollLeft || 0,
      startScrollTop: scrollRef.current?.scrollTop || 0,
      startX: Number(element.x ?? 40),
      startY: Number(element.y ?? 40),
    };
  };

  const beginResize = (event: ReactPointerEvent<HTMLElement>, element: ExcalidrawElement) => {
    if (readOnly || element.type === 'arrow' || element.type === 'text') return;
    event.preventDefault();
    event.stopPropagation();
    setSelectedElementId(element.id);
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // Pointer capture is optional in test environments.
    }
    const box = elementBox(element);
    resizeRef.current = {
      id: element.id,
      startClientX: event.clientX,
      startClientY: event.clientY,
      startScrollLeft: scrollRef.current?.scrollLeft || 0,
      startScrollTop: scrollRef.current?.scrollTop || 0,
      startWidth: box.width,
      startHeight: box.height,
    };
  };

  const beginCanvasPan = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    const target = event.target as HTMLElement;
    if (target.closest('[data-architecture-element="true"]')) return;
    setSelectedElementId('');
    setConnectSourceId('');
    setConnectSourceAnchor(null);
    panRef.current = {
      startClientX: event.clientX,
      startClientY: event.clientY,
      startScrollLeft: event.currentTarget.scrollLeft,
      startScrollTop: event.currentTarget.scrollTop,
    };
    setIsPanning(true);
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // Pointer capture is a convenience, not a requirement.
    }
  };

  const handleCanvasPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const pan = panRef.current;
    if (pan) {
      event.currentTarget.scrollLeft = pan.startScrollLeft - (event.clientX - pan.startClientX);
      event.currentTarget.scrollTop = pan.startScrollTop - (event.clientY - pan.startClientY);
      return;
    }
    const resize = resizeRef.current;
    if (resize && !readOnly) {
      autoScrollNearEdge(event);
      const viewport = scrollRef.current;
      const scrollDeltaX = viewport ? viewport.scrollLeft - resize.startScrollLeft : 0;
      const scrollDeltaY = viewport ? viewport.scrollTop - resize.startScrollTop : 0;
      const nextWidth = snapSizeToGrid(resize.startWidth + (event.clientX - resize.startClientX + scrollDeltaX) / zoom, MIN_NODE_WIDTH);
      const nextHeight = snapSizeToGrid(resize.startHeight + (event.clientY - resize.startClientY + scrollDeltaY) / zoom, MIN_NODE_HEIGHT);
      updateElements(elements.map((item) => (item.id === resize.id ? { ...item, width: nextWidth, height: nextHeight } : item)));
      return;
    }
    const drag = dragRef.current;
    if (!drag || readOnly) return;
    const viewport = scrollRef.current;
    autoScrollNearEdge(event);
    const scrollDeltaX = viewport ? viewport.scrollLeft - drag.startScrollLeft : 0;
    const scrollDeltaY = viewport ? viewport.scrollTop - drag.startScrollTop : 0;
    const nextX = snapToGrid(drag.startX + (event.clientX - drag.startClientX + scrollDeltaX) / zoom);
    const nextY = snapToGrid(drag.startY + (event.clientY - drag.startClientY + scrollDeltaY) / zoom);
    updateElements(elements.map((item) => (item.id === drag.id ? { ...item, x: nextX, y: nextY } : item)));
  };

  const endPointerInteraction = () => {
    dragRef.current = null;
    resizeRef.current = null;
    panRef.current = null;
    setIsPanning(false);
  };

  const createConnection = (sourceId: string, targetId: string, requestedSourceAnchor?: ConnectionAnchor | null, requestedTargetAnchor?: ConnectionAnchor | null) => {
    const source = elements.find((item) => item.id === sourceId);
    const target = elements.find((item) => item.id === targetId);
    if (!source || !target) return;
    const anchors = bestAnchorsBetween(source, target);
    const sourceAnchor = requestedSourceAnchor || anchors.sourceAnchor;
    const targetAnchor = requestedTargetAnchor || anchors.targetAnchor;
    const sourcePoint = anchorPoint(elementBox(source), sourceAnchor);
    const targetPoint = anchorPoint(elementBox(target), targetAnchor);
    const next: ExcalidrawElement = {
      id: nextElementId('edge'),
      type: 'arrow',
      x: Math.min(sourcePoint.x, targetPoint.x),
      y: Math.min(sourcePoint.y, targetPoint.y),
      width: Math.max(80, Math.abs(targetPoint.x - sourcePoint.x)),
      height: Math.max(2, Math.abs(targetPoint.y - sourcePoint.y)),
      text: '',
      displayType: 'Edge',
      architectureKind: 'relationship',
      strokeColor: '#94a3b8',
      sourceElementId: sourceId,
      targetElementId: targetId,
      sourceAnchor,
      targetAnchor,
      connectionType: 'direct',
      points: [[0, 0], [1, 1]],
    };
    updateElements([...elements, next]);
    setSelectedElementId(next.id);
  };

  const handleNodeClick = (element: ExcalidrawElement) => {
    setSelectedElementId(element.id);
    if (!connectMode || readOnly || element.type === 'arrow') return;
    if (!connectSourceId) {
      setConnectSourceId(element.id);
      setConnectSourceAnchor(null);
      return;
    }
    if (connectSourceId === element.id) {
      setConnectSourceId('');
      setConnectSourceAnchor(null);
      return;
    }
    createConnection(connectSourceId, element.id, connectSourceAnchor, null);
    setConnectSourceId('');
    setConnectSourceAnchor(null);
    setConnectMode(false);
  };

  const handleAnchorClick = (event: ReactPointerEvent<HTMLElement> | ReactMouseEvent<HTMLElement>, element: ExcalidrawElement, anchor: ConnectionAnchor) => {
    event.preventDefault();
    event.stopPropagation();
    setSelectedElementId(element.id);
    if (readOnly || element.type === 'arrow') return;
    if (!connectMode || !connectSourceId) {
      setConnectMode(true);
      setConnectSourceId(element.id);
      setConnectSourceAnchor(anchor);
      return;
    }
    if (connectSourceId === element.id && connectSourceAnchor === anchor) {
      setConnectSourceId('');
      setConnectSourceAnchor(null);
      return;
    }
    createConnection(connectSourceId, element.id, connectSourceAnchor, anchor);
    setConnectSourceId('');
    setConnectSourceAnchor(null);
    setConnectMode(false);
  };

  const fitToView = () => {
    const viewport = scrollRef.current;
    const bounds = getElementsBounds(elements);
    if (!viewport || !bounds) {
      setZoom(1);
      return;
    }
    const nextZoom = clampZoom(Math.min(
      viewport.clientWidth / Math.max(1, bounds.width + 180),
      viewport.clientHeight / Math.max(1, bounds.height + 180),
    ));
    setZoom(nextZoom);
    window.requestAnimationFrame(() => {
      viewport.scrollLeft = Math.max(0, (bounds.minX - 90) * nextZoom);
      viewport.scrollTop = Math.max(0, (bounds.minY - 90) * nextZoom);
    });
  };

  const handleLinkedInterfaceToggle = (interfaceId: string, checked: boolean) => {
    if (!selectedElement) return;
    const refs = linkedInterfaceRefs(selectedElement);
    const nextRefs = checked ? uniqueRefs([...refs, interfaceId]) : refs.filter((ref) => ref !== interfaceId);
    const linkedLabels = interfaceOptions
      .filter((item) => nextRefs.includes(item.id))
      .map((item) => item.label);
    updateSelected({
      ...withLinkedInterfaceRefs(selectedElement, nextRefs),
      text: !selectedElement.text?.trim() ? linkedLabels.join(', ') : selectedElement.text,
    });
  };

  const nodeLabel = (element: ExcalidrawElement | null | undefined): string => {
    if (!element) return 'Not set';
    const entityDetail = element.linkedEntityId ? entityDetailsByRef.get(refKey(element.linkedEntityId)) : null;
    return entityDetail?.label || elementName(element, 'Unnamed');
  };

  const renderDetailsModal = () => {
    const detailElement = elements.find((item) => item.id === detailsElementId);
    if (!detailElement) return null;
    const isConnection = detailElement.type === 'arrow';
    const source = isConnection && detailElement.sourceElementId ? elements.find((item) => item.id === detailElement.sourceElementId) : null;
    const target = isConnection && detailElement.targetElementId ? elements.find((item) => item.id === detailElement.targetElementId) : null;
    const linkedInterfaceDetails = linkedInterfaceRefs(detailElement)
      .map((ref) => ({ ref, detail: interfaceDetailsByRef.get(refKey(ref)) }))
      .filter((item) => item.ref);
    const entityDetail = !isConnection && detailElement.linkedEntityId
      ? entityDetailsByRef.get(refKey(detailElement.linkedEntityId))
      : null;
    const entity = entityDetail?.item;
    const mockupLabel = detailElement.linkedMockupId
      ? mockupOptions.find((item) => item.id === detailElement.linkedMockupId)?.label || detailElement.linkedMockupId
      : '';
    const title = isConnection ? elementName(detailElement, 'Connection') : elementName(detailElement, entityDetail?.label || 'Unnamed');

    return (
      <div
        className="fixed inset-0 z-[10000] flex items-center justify-center bg-black/60 p-4"
        onMouseDown={(event) => {
          if (event.target === event.currentTarget) setDetailsElementId('');
        }}
      >
        <div
          role="dialog"
          aria-modal="true"
          aria-label={isConnection ? 'Connection details' : 'Entity details'}
          className="w-full max-w-3xl max-h-[86vh] overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 shadow-2xl"
        >
          <div className="flex items-start justify-between gap-4 border-b border-gray-200 dark:border-gray-700 px-4 py-3">
            <div className="min-w-0">
              <p className="text-xs font-semibold uppercase tracking-wide text-cyan-600 dark:text-cyan-300">
                {isConnection ? 'Connection details' : 'Entity details'}
              </p>
              <h3 className="truncate text-lg font-semibold text-gray-950 dark:text-gray-50">{title}</h3>
            </div>
            <button type="button" onClick={() => setDetailsElementId('')} className="btn btn-secondary text-sm">
              Close
            </button>
          </div>
          <div className="max-h-[calc(86vh-76px)] overflow-y-auto p-4 [scrollbar-gutter:stable]">
            {isConnection ? (
              <div className="space-y-4">
                <dl className="rounded-lg border border-gray-200 dark:border-gray-700 px-3">
                  <DetailRow label="Element id">{detailElement.id}</DetailRow>
                  <DetailRow label="Source">{nodeLabel(source)}</DetailRow>
                  <DetailRow label="Target">{nodeLabel(target)}</DetailRow>
                  <DetailRow label="Connection type">{normalizeConnectionType(detailElement.connectionType)}</DetailRow>
                  <DetailRow label="Interfaces">{linkedInterfaceDetails.length > 0 ? `${linkedInterfaceDetails.length}` : ''}</DetailRow>
                </dl>
                <div className="space-y-3">
                  {linkedInterfaceDetails.length === 0 ? (
                    <p className="rounded-lg border border-dashed border-gray-300 dark:border-gray-700 p-3 text-sm text-gray-500 dark:text-gray-400">
                      No interface contracts are linked to this connection.
                    </p>
                  ) : linkedInterfaceDetails.map(({ ref, detail }) => {
                    const item = detail?.item;
                    return (
                      <section key={ref} className="rounded-lg border border-gray-200 dark:border-gray-700 p-3">
                        <div className="mb-2">
                          <p className="text-sm font-semibold text-gray-950 dark:text-gray-50">{detail?.label || ref}</p>
                          <p className="text-xs text-gray-500 dark:text-gray-400">{ref}</p>
                        </div>
                        <dl>
                          <DetailRow label="Endpoint">{item?.endpoint}</DetailRow>
                          <DetailRow label="Description">{item?.description}</DetailRow>
                          <DetailRow label="Direction">{interfaceDirectionLabel(item?.direction)}</DetailRow>
                          <DetailRow label="Protocol">{item?.protocol}</DetailRow>
                          <DetailRow label="Contract type">{item?.contract_type}</DetailRow>
                          <DetailRow label="Participants">{detailText(item?.participants)}</DetailRow>
                          <DetailRow label="Request schema"><DetailCode value={item?.request_schema} /></DetailRow>
                          <DetailRow label="Response schema"><DetailCode value={item?.response_schema} /></DetailRow>
                          <DetailRow label="Event schema"><DetailCode value={item?.event_schema} /></DetailRow>
                          <DetailRow label="Error contract"><DetailCode value={item?.error_contract} /></DetailRow>
                          <DetailRow label="Schema ref">{item?.schema_ref}</DetailRow>
                          <DetailRow label="Notes">{item?.notes}</DetailRow>
                        </dl>
                      </section>
                    );
                  })}
                </div>
              </div>
            ) : (
              <dl className="rounded-lg border border-gray-200 dark:border-gray-700 px-3">
                <DetailRow label="Element id">{detailElement.id}</DetailRow>
                <DetailRow label="Canvas label">{elementName(detailElement, entityDetail?.label || 'Unnamed')}</DetailRow>
                <DetailRow label="Canvas type">{elementTypeLabel(detailElement)}</DetailRow>
                <DetailRow label="Linked entity">{entityDetail?.label || detailElement.linkedEntityId}</DetailRow>
                <DetailRow label="Entity type">{entity?.entity_type}</DetailRow>
                <DetailRow label="Responsibility">{entity?.responsibility}</DetailRow>
                <DetailRow label="Boundary">{entity?.boundaries}</DetailRow>
                <DetailRow label="Technologies">{detailText(entity?.technologies)}</DetailRow>
                <DetailRow label="Relationships">{detailText(entity?.relationships)}</DetailRow>
                <DetailRow label="Linked screen">{mockupLabel}</DetailRow>
                <DetailRow label="Notes">{entity?.notes}</DetailRow>
              </dl>
            )}
          </div>
        </div>
      </div>
    );
  };

  const renderEdge = (element: ExcalidrawElement) => {
    const selected = element.id === selectedElement?.id;
    const source = element.sourceElementId ? elements.find((item) => item.id === element.sourceElementId) : null;
    const target = element.targetElementId ? elements.find((item) => item.id === element.targetElementId) : null;
    const connected = Boolean(source && target);
    const sourceBox = source ? elementBox(source) : null;
    const targetBox = target ? elementBox(target) : null;
    const anchors = source && target ? bestAnchorsBetween(source, target) : null;
    const sourceAnchor = (element.sourceAnchor || anchors?.sourceAnchor || 'right') as ConnectionAnchor;
    const targetAnchor = (element.targetAnchor || anchors?.targetAnchor || 'left') as ConnectionAnchor;
    const sourcePoint = sourceBox ? anchorPoint(sourceBox, sourceAnchor) : null;
    const targetPoint = targetBox ? anchorPoint(targetBox, targetAnchor) : null;
    const box = elementBox(element);
    const edgePadding = 28;
    const left = connected && sourcePoint && targetPoint ? Math.min(sourcePoint.x, targetPoint.x) - edgePadding : box.x;
    const top = connected && sourcePoint && targetPoint ? Math.min(sourcePoint.y, targetPoint.y) - edgePadding : box.y - 16;
    const width = connected && sourcePoint && targetPoint ? Math.max(32, Math.abs(targetPoint.x - sourcePoint.x) + edgePadding * 2) : box.width;
    const height = connected && sourcePoint && targetPoint ? Math.max(32, Math.abs(targetPoint.y - sourcePoint.y) + edgePadding * 2) : 32;
    const x1 = connected && sourcePoint && targetPoint ? sourcePoint.x - left : 0;
    const y1 = connected && sourcePoint && targetPoint ? sourcePoint.y - top : height / 2;
    const x2 = connected && sourcePoint && targetPoint ? targetPoint.x - left : width - 8;
    const y2 = connected && sourcePoint && targetPoint ? targetPoint.y - top : height / 2;
    const stroke = element.strokeColor || '#94a3b8';
    const linkedInterfaces = interfaceOptions.filter((item) => linkedInterfaceRefs(element).includes(item.id));
    const linkedInterfaceLabel = linkedInterfaces.length === 1
      ? linkedInterfaces[0].label
      : linkedInterfaces.length > 1
        ? `${linkedInterfaces.length} interfaces`
        : 'Connection';
    const label = elementName(element, linkedInterfaceLabel);
    const connectionType = normalizeConnectionType(element.connectionType);
    const midX = (x1 + x2) / 2;
    const pathPoints = connectionType === 'elbow'
      ? [{ x: x1, y: y1 }, { x: midX, y: y1 }, { x: midX, y: y2 }, { x: x2, y: y2 }]
      : [{ x: x1, y: y1 }, { x: x2, y: y2 }];
    const pathD = pathPoints.map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x} ${point.y}`).join(' ');
    const labelPoint = pathMiddlePoint(pathPoints);
    const direction = combinedInterfaceDirection(linkedInterfaces.map((item) => item.direction));
    const markerUrl = `url(#arrowhead-${element.id})`;
    const markerStart = direction === 'target_to_source' || direction === 'bidirectional' ? markerUrl : undefined;
    const markerEnd = direction === 'source_to_target' || direction === 'bidirectional' ? markerUrl : undefined;

    return (
      <button
        type="button"
        key={element.id}
        data-architecture-element="true"
        data-testid={`architecture-element-${element.id}`}
        onPointerDown={(event) => beginDrag(event, element)}
        onClick={() => setSelectedElementId(element.id)}
        onDoubleClick={(event) => openElementDetails(event, element)}
        className={`absolute text-gray-700 dark:text-gray-200 ${connected || readOnly ? 'cursor-pointer' : 'cursor-move'} ${selected ? 'ring-2 ring-cyan-500 rounded' : ''}`}
        style={{ left, top, width, height }}
        title={label}
      >
        <svg className="absolute inset-0 overflow-visible" width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
          <defs>
            <marker id={`arrowhead-${element.id}`} markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto-start-reverse">
              <path d="M 0 0 L 8 4 L 0 8 z" fill={stroke} />
            </marker>
          </defs>
          <path
            d={pathD}
            stroke="transparent"
            strokeWidth="14"
            fill="none"
          />
          <path
            d={pathD}
            stroke={stroke}
            strokeWidth="2"
            strokeDasharray={element.lineStyle === 'dashed' ? '7 5' : undefined}
            markerStart={markerStart}
            markerEnd={markerEnd}
            fill="none"
          />
        </svg>
        {(linkedInterfaces.length > 0 || (label && label !== 'Connection')) ? (
          <span
            className="absolute max-w-[80%] -translate-x-1/2 -translate-y-1/2 rounded bg-gray-50 dark:bg-gray-950 px-1 py-0.5 text-center leading-tight shadow-sm"
            style={{ left: labelPoint.x, top: labelPoint.y }}
          >
            {linkedInterfaces.length > 0 ? linkedInterfaces.map((item) => (
              <span key={item.id} className="block max-w-full truncate">
                <span className="block truncate text-[11px] font-medium">{item.label}</span>
                {(item.endpoint || item.protocol) && (
                  <span className="block truncate text-[10px] font-medium uppercase text-gray-500 dark:text-gray-400">
                    {[item.endpoint, item.protocol].filter(Boolean).join(' / ')}
                  </span>
                )}
              </span>
            )) : (
              <span className="block truncate text-[11px] font-medium">{label}</span>
            )}
          </span>
        ) : null}
      </button>
    );
  };

  const renderNode = (element: ExcalidrawElement) => {
    const box = elementBox(element);
    const selected = element.id === selectedElement?.id;
    const usesDefaultFill = isDefaultLightFill(element.backgroundColor);
    const fallbackLabel = entityOptions.find((item) => item.id === element.linkedEntityId)?.label
      || mockupOptions.find((item) => item.id === element.linkedMockupId)?.label
      || 'Unnamed';
    const name = elementName(element, fallbackLabel);
    const type = elementTypeLabel(element);
    const NodeIcon = iconForElement(element);
    const isConnectSource = connectSourceId === element.id;
    const renderAnchor = (anchor: ConnectionAnchor) => {
      if (element.type === 'text') return null;
      const active = connectSourceId === element.id && connectSourceAnchor === anchor;
      const visible = connectMode || selected || isConnectSource;
      const positionClass = anchor === 'top'
        ? 'left-1/2 top-0 -translate-x-1/2 -translate-y-1/2'
        : anchor === 'right'
          ? 'right-0 top-1/2 translate-x-1/2 -translate-y-1/2'
          : anchor === 'bottom'
            ? 'left-1/2 bottom-0 -translate-x-1/2 translate-y-1/2'
            : 'left-0 top-1/2 -translate-x-1/2 -translate-y-1/2';
      return (
        <span
          key={anchor}
          data-architecture-anchor="true"
          data-testid={`architecture-anchor-${element.id}-${anchor}`}
          className={`absolute ${positionClass} h-3 w-3 rounded-full border border-cyan-400 bg-gray-950 shadow-sm transition-opacity ${visible ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'} ${active ? 'ring-2 ring-amber-400' : ''} ${readOnly ? 'pointer-events-none' : 'cursor-crosshair'}`}
          onPointerDown={(event) => {
            event.preventDefault();
            event.stopPropagation();
          }}
          onClick={(event) => handleAnchorClick(event, element, anchor)}
          title={`${anchor} connector`}
        />
      );
    };

    if (element.type === 'text') {
      return (
        <button
          type="button"
          key={element.id}
          data-architecture-element="true"
          data-testid={`architecture-element-${element.id}`}
          onPointerDown={(event) => beginDrag(event, element)}
          onClick={() => handleNodeClick(element)}
          onDoubleClick={(event) => openElementDetails(event, element)}
          className={`absolute rounded-md border-2 shadow-sm flex items-center justify-center px-2 text-sm font-medium text-gray-900 dark:text-gray-100 bg-transparent ${readOnly ? 'cursor-default' : 'cursor-move'} ${selected ? 'ring-2 ring-cyan-500' : ''}`}
          style={{ left: box.x, top: box.y, width: box.width, height: box.height, borderColor: element.strokeColor || '#334155' }}
          title={name}
        >
          <span className="truncate">{name}</span>
        </button>
      );
    }

    return (
      <button
        type="button"
        key={element.id}
        data-architecture-element="true"
        data-testid={`architecture-element-${element.id}`}
        onPointerDown={(event) => beginDrag(event, element)}
        onClick={() => handleNodeClick(element)}
        onDoubleClick={(event) => openElementDetails(event, element)}
        className={`absolute group rounded-md border-2 shadow-sm flex items-center justify-center gap-2 px-3 text-gray-900 dark:text-gray-100 ${readOnly ? 'cursor-default' : connectMode ? 'cursor-crosshair' : 'cursor-move'} ${usesDefaultFill ? 'bg-white dark:bg-gray-900' : ''} ${selected ? 'ring-2 ring-cyan-500' : ''} ${isConnectSource ? 'ring-2 ring-amber-400' : ''}`}
        style={{
          left: box.x,
          top: box.y,
          width: box.width,
          height: box.height,
          borderColor: element.strokeColor || '#0891b2',
          backgroundColor: usesDefaultFill ? undefined : element.backgroundColor,
        }}
        title={`${name} (${type})`}
      >
        <NodeIcon size={17} className="shrink-0 text-gray-500 dark:text-gray-300" />
        <span className="min-w-0 flex flex-col items-start leading-tight">
          <span className="max-w-full truncate text-sm font-semibold">{name}</span>
          <span className="max-w-full truncate text-[10px] font-medium uppercase text-gray-500 dark:text-gray-400">{type}</span>
        </span>
        {selected && !readOnly && (
          <span
            data-architecture-resize-handle="true"
            className="absolute -bottom-2 -right-2 h-4 w-4 rounded-sm border border-cyan-500 bg-white dark:bg-gray-900 shadow cursor-nwse-resize"
            onPointerDown={(event) => beginResize(event, element)}
            title="Resize"
          />
        )}
        {(['top', 'right', 'bottom', 'left'] as ConnectionAnchor[]).map(renderAnchor)}
      </button>
    );
  };

  if (!diagram) {
    return (
      <div className="h-72 border border-dashed border-gray-300 dark:border-gray-700 rounded-lg flex items-center justify-center text-sm text-gray-500 dark:text-gray-400">
        No diagram selected
      </div>
    );
  }

  const showElementMenu = selectedElement?.type === 'arrow';

  return (
    <div className={`w-full max-w-full min-w-0 min-h-[460px] h-full border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden bg-white dark:bg-gray-900 flex flex-col ${isFullscreen ? 'fixed inset-4 z-[9999] shadow-2xl' : ''}`}>
      <div className="shrink-0 px-3 py-2 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between gap-2 min-w-0">
        <div className="flex items-center gap-1 shrink-0">
          <button
            type="button"
            onClick={() => setMode('visual')}
            className={`p-1.5 rounded ${mode === 'visual' ? 'bg-cyan-100 text-cyan-700 dark:bg-cyan-900/40 dark:text-cyan-200' : 'text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800'}`}
            title="Visual editor"
          >
            <MousePointer2 size={15} />
          </button>
          <button
            type="button"
            onClick={() => setMode('raw')}
            className={`p-1.5 rounded ${mode === 'raw' ? 'bg-cyan-100 text-cyan-700 dark:bg-cyan-900/40 dark:text-cyan-200' : 'text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800'}`}
            title="Raw payload"
          >
            <Code2 size={15} />
          </button>
        </div>

        {mode === 'visual' && (
          <div className="flex items-center gap-1 flex-wrap justify-end min-w-0 overflow-x-auto">
            <button
              type="button"
              onClick={() => setShowGrid((current) => !current)}
              className={`p-1.5 rounded ${showGrid ? 'bg-cyan-100 text-cyan-700 dark:bg-cyan-900/40 dark:text-cyan-200' : 'text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800'}`}
              title={showGrid ? 'Hide grid' : 'Show grid'}
            >
              <Grid3X3 size={15} />
            </button>
            <button type="button" onClick={() => setZoom((current) => clampZoom(current - ZOOM_STEP))} className={iconButtonClass} title="Zoom out">
              <ZoomOut size={15} />
            </button>
            <span className="w-11 text-center text-xs font-medium text-gray-500 dark:text-gray-400">{Math.round(zoom * 100)}%</span>
            <button type="button" onClick={() => setZoom((current) => clampZoom(current + ZOOM_STEP))} className={iconButtonClass} title="Zoom in">
              <ZoomIn size={15} />
            </button>
            <button type="button" onClick={() => setZoom(1)} className={iconButtonClass} title="Reset zoom">
              <RotateCcw size={15} />
            </button>
            <button type="button" onClick={fitToView} className={iconButtonClass} title="Fit to view">
              <Focus size={15} />
            </button>
            <button type="button" onClick={() => setIsFullscreen((current) => !current)} className={iconButtonClass} title={isFullscreen ? 'Exit fullscreen' : 'Fullscreen diagram'}>
              {isFullscreen ? <Minimize2 size={15} /> : <Fullscreen size={15} />}
            </button>
          </div>
        )}
      </div>

      {mode === 'raw' ? (
        <div className="min-h-0 flex-1 overflow-y-auto p-3 space-y-2 [scrollbar-gutter:stable]">
          <textarea
            value={rawDraft}
            onChange={(event) => setRawDraft(event.target.value)}
            readOnly={readOnly}
            rows={16}
            className="w-full px-3 py-2 text-xs font-mono border border-gray-300 dark:border-gray-700 rounded-lg bg-gray-50 dark:bg-gray-950 text-gray-900 dark:text-gray-100 resize-y"
          />
          {!readOnly && (
            <div className="flex justify-end">
              <button type="button" onClick={applyRawPayload} className="btn btn-secondary text-sm">
                Apply Raw
              </button>
            </div>
          )}
        </div>
      ) : (
        <div className={showElementMenu ? 'grid grid-cols-[minmax(0,1fr)_260px] min-h-0 flex-1 min-w-0 overflow-hidden' : 'min-h-0 flex-1 min-w-0 overflow-hidden'}>
          <div
            ref={scrollRef}
            data-testid="architecture-canvas"
            className={`relative min-w-0 max-w-full min-h-[460px] ${isFullscreen ? 'h-[calc(100vh-150px)]' : 'h-full'} bg-gray-50 dark:bg-gray-950 overflow-auto [scrollbar-gutter:stable] ${isPanning ? 'cursor-grabbing' : 'cursor-grab'}`}
            onPointerDown={beginCanvasPan}
            onPointerMove={handleCanvasPointerMove}
            onPointerUp={endPointerInteraction}
            onPointerCancel={endPointerInteraction}
            onPointerLeave={endPointerInteraction}
          >
            <div className="relative" style={{ width: canvasSize.width * zoom, height: canvasSize.height * zoom }}>
              <div
                className="absolute left-0 top-0"
                style={{
                  width: canvasSize.width,
                  height: canvasSize.height,
                  transform: `scale(${zoom})`,
                  transformOrigin: '0 0',
                  backgroundImage: showGrid
                    ? 'linear-gradient(rgba(148, 163, 184, 0.45) 1px, transparent 1px), linear-gradient(90deg, rgba(148, 163, 184, 0.45) 1px, transparent 1px)'
                    : undefined,
                  backgroundSize: '24px 24px',
                }}
              >
                {edgeElements.map(renderEdge)}
                {nodeElements.map(renderNode)}
              </div>
            </div>
          </div>

          {showElementMenu && selectedElement && (
            <aside className={`min-h-0 border-l border-gray-200 dark:border-gray-700 p-3 space-y-3 bg-white dark:bg-gray-900 overflow-y-auto [scrollbar-gutter:stable] ${isFullscreen ? 'max-h-[calc(100vh-150px)]' : 'max-h-full'}`}>
              <div className="flex items-center justify-between">
                <p className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
                  Connection
                </p>
                {!readOnly && (
                  <button type="button" onClick={() => deleteElementById(selectedElement.id)} className="p-1 text-gray-400 hover:text-red-500 rounded" title="Delete element">
                    <Trash2 size={14} />
                  </button>
                )}
              </div>
              <div className="block">
                <span className={labelClass}>Linked Interfaces</span>
                <div className="mt-1 max-h-40 overflow-y-auto rounded border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-950 p-1 [scrollbar-gutter:stable]">
                  {interfaceOptions.length === 0 ? (
                    <p className="px-2 py-1 text-xs text-gray-500 dark:text-gray-400">No interfaces registered</p>
                  ) : interfaceOptions.map((item) => {
                    const checked = linkedInterfaceRefs(selectedElement).includes(item.id);
                    return (
                      <label key={item.id} className="flex items-start gap-2 rounded px-2 py-1 text-xs text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800">
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={(event) => handleLinkedInterfaceToggle(item.id, event.target.checked)}
                          disabled={readOnly}
                          className="mt-0.5"
                        />
                        <span className="min-w-0">
                          <span className="block truncate font-medium">{item.label}</span>
                          {(item.endpoint || item.protocol) && (
                            <span className="block truncate text-[11px] uppercase text-gray-500 dark:text-gray-400">
                              {[item.endpoint, item.protocol].filter(Boolean).join(' / ')}
                            </span>
                          )}
                        </span>
                      </label>
                    );
                  })}
                </div>
              </div>
              <label className="block">
                <span className={labelClass}>Connection type</span>
                <select
                  value={normalizeConnectionType(selectedElement.connectionType)}
                  onChange={(event) => updateSelected({ connectionType: event.target.value as ConnectionType })}
                  disabled={readOnly}
                  className={inputClass}
                >
                  <option value="direct">Direct</option>
                  <option value="elbow">Curved</option>
                </select>
              </label>
            </aside>
          )}
        </div>
      )}
      {renderDetailsModal()}
    </div>
  );
}

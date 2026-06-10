/**
 * SigmaMiniMap — custom minimap overlay for the Sigma-based GraphCanvas.
 *
 * React Flow shipped a MiniMap out of the box; Sigma does not. This keeps
 * the capability: node dots colored per NODE_TYPE_CONFIG, a viewport
 * rectangle that tracks the camera, and click-to-pan. Drawn on a plain
 * 2D canvas from the live graphology coordinates after every sigma render
 * (throttled via requestAnimationFrame).
 */

import { useEffect, useRef, type MutableRefObject } from 'react';
import type { Sigma } from 'sigma';
import type { KGNodeType } from '@/types/knowledge-graph';
import { NODE_TYPE_CONFIG } from '@/types/knowledge-graph';

const WIDTH = 160;
const HEIGHT = 100;
const PAD = 8;

interface Props {
  sigmaRef: MutableRefObject<Sigma | null>;
  isDark: boolean;
  /** Any string that changes when the drawn content may have changed
   *  (node count, layout epoch, settling state) — forces a redraw even if
   *  sigma didn't emit afterRender yet. */
  epoch: string;
}

interface Bounds {
  minX: number;
  maxX: number;
  minY: number;
  maxY: number;
}

function graphBounds(sigma: Sigma): Bounds | null {
  const graph = sigma.getGraph();
  if (graph.order === 0) return null;
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  graph.forEachNode((_id, attr) => {
    const x = attr.x as number;
    const y = attr.y as number;
    if (x < minX) minX = x;
    if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    if (y > maxY) maxY = y;
  });
  if (!Number.isFinite(minX)) return null;
  // Degenerate single-point graphs still need a non-zero span.
  if (maxX - minX < 1e-6) {
    minX -= 1;
    maxX += 1;
  }
  if (maxY - minY < 1e-6) {
    minY -= 1;
    maxY += 1;
  }
  return { minX, maxX, minY, maxY };
}

function toMini(bounds: Bounds, x: number, y: number): { mx: number; my: number } {
  const sx = (WIDTH - PAD * 2) / (bounds.maxX - bounds.minX);
  const sy = (HEIGHT - PAD * 2) / (bounds.maxY - bounds.minY);
  return {
    mx: PAD + (x - bounds.minX) * sx,
    // Graph y grows up, canvas y grows down.
    my: HEIGHT - PAD - (y - bounds.minY) * sy,
  };
}

export function SigmaMiniMap({ sigmaRef, isDark, epoch }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const rafRef = useRef<number | null>(null);
  const isDarkRef = useRef(isDark);
  isDarkRef.current = isDark;

  useEffect(() => {
    const sigma = sigmaRef.current;
    const canvas = canvasRef.current;
    if (!sigma || !canvas) return;

    const draw = () => {
      rafRef.current = null;
      const ctx = canvas.getContext('2d');
      if (!ctx) return;
      const dark = isDarkRef.current;
      ctx.clearRect(0, 0, WIDTH, HEIGHT);
      const bounds = graphBounds(sigma);
      if (!bounds) return;

      // Node dots.
      const graph = sigma.getGraph();
      graph.forEachNode((_id, attr) => {
        const { mx, my } = toMini(bounds, attr.x as number, attr.y as number);
        const cfg = NODE_TYPE_CONFIG[attr.nodeType as KGNodeType];
        ctx.fillStyle = cfg ? (dark ? cfg.darkColor : cfg.color) : '#6B7280';
        ctx.beginPath();
        ctx.arc(mx, my, 1.6, 0, Math.PI * 2);
        ctx.fill();
      });

      // Viewport rectangle — current camera window in graph coords.
      const { width, height } = sigma.getDimensions();
      if (width > 0 && height > 0) {
        const tl = sigma.viewportToGraph({ x: 0, y: 0 });
        const br = sigma.viewportToGraph({ x: width, y: height });
        const a = toMini(bounds, tl.x, tl.y);
        const b = toMini(bounds, br.x, br.y);
        const rx = Math.min(a.mx, b.mx);
        const ry = Math.min(a.my, b.my);
        const rw = Math.abs(b.mx - a.mx);
        const rh = Math.abs(b.my - a.my);
        ctx.strokeStyle = dark ? 'rgba(148,163,184,0.9)' : 'rgba(71,85,105,0.9)';
        ctx.lineWidth = 1;
        ctx.strokeRect(rx, ry, Math.max(rw, 4), Math.max(rh, 4));
        ctx.fillStyle = dark ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.05)';
        ctx.fillRect(rx, ry, Math.max(rw, 4), Math.max(rh, 4));
      }
    };

    const requestDraw = () => {
      if (rafRef.current !== null) return;
      rafRef.current = window.requestAnimationFrame(draw);
    };

    sigma.on('afterRender', requestDraw);
    requestDraw();
    return () => {
      sigma.off('afterRender', requestDraw);
      if (rafRef.current !== null) window.cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    };
    // `epoch` forces re-subscription/redraw when the parent knows content changed.
  }, [sigmaRef, epoch]);

  // Click-to-pan: map the minimap point back to graph coords, then to the
  // camera's framed space (compose graphToViewport with viewportToFramedGraph
  // so the conversion is camera-correct).
  const handleClick = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const sigma = sigmaRef.current;
    const canvas = canvasRef.current;
    if (!sigma || !canvas) return;
    const bounds = graphBounds(sigma);
    if (!bounds) return;
    const rect = canvas.getBoundingClientRect();
    const mx = ((e.clientX - rect.left) / rect.width) * WIDTH;
    const my = ((e.clientY - rect.top) / rect.height) * HEIGHT;
    const gx = bounds.minX + ((mx - PAD) / (WIDTH - PAD * 2)) * (bounds.maxX - bounds.minX);
    const gy = bounds.minY + ((HEIGHT - PAD - my) / (HEIGHT - PAD * 2)) * (bounds.maxY - bounds.minY);
    const framed = sigma.viewportToFramedGraph(sigma.graphToViewport({ x: gx, y: gy }));
    sigma.getCamera().animate({ x: framed.x, y: framed.y }, { duration: 250 });
  };

  return (
    <canvas
      ref={canvasRef}
      width={WIDTH}
      height={HEIGHT}
      onClick={handleClick}
      data-testid="kg-minimap"
      aria-label="Graph minimap"
      className="absolute bottom-12 right-3 z-20 cursor-pointer rounded-md border shadow-sm"
      style={{
        width: WIDTH,
        height: HEIGHT,
        backgroundColor: isDark ? '#0f172a' : '#ffffff',
        borderColor: isDark ? '#334155' : '#e5e7eb',
      }}
    />
  );
}

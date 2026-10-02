import { h, svg, badge, fmtDuration } from './dom.js';

const NODE_W = 172, NODE_H = 56, GAP_X = 60, GAP_Y = 20, PAD = 10;

/** Layers by longest dependency chain, so parallel branches sit in the same column. */
export function layout(steps) {
  const byName = new Map(steps.map((s) => [s.step, s]));
  const level = new Map();
  const depth = (name) => {
    if (level.has(name)) return level.get(name);
    const deps = (byName.get(name)?.depends_on || []).filter((d) => byName.has(d));
    const value = deps.length ? 1 + Math.max(...deps.map(depth)) : 0;
    level.set(name, value);
    return value;
  };
  steps.forEach((s) => depth(s.step));
  const columns = [];
  steps.forEach((s) => {
    const l = level.get(s.step);
    (columns[l] ||= []).push(s);
  });
  const pos = new Map();
  columns.forEach((col, x) => col.forEach((s, y) => {
    pos.set(s.step, { x: PAD + x * (NODE_W + GAP_X), y: PAD + y * (NODE_H + GAP_Y) });
  }));
  const width = PAD * 2 + columns.length * NODE_W + (columns.length - 1) * GAP_X;
  const height = PAD * 2 + Math.max(...columns.map((c) => c.length)) * (NODE_H + GAP_Y) - GAP_Y;
  return { pos, width, height };
}

export function dagSvg(steps, selected, onSelect) {
  const { pos, width, height } = layout(steps);
  const edges = [];
  for (const s of steps) {
    for (const dep of s.depends_on || []) {
      const a = pos.get(dep), b = pos.get(s.step);
      if (!a || !b) continue;
      const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2;
      const mid = (x2 - x1) / 2;
      edges.push(svg('path', {
        class: 'edge', 'marker-end': 'url(#arrow)',
        d: `M${x1} ${y1} C${x1 + mid} ${y1}, ${x2 - mid} ${y2}, ${x2} ${y2}`,
      }));
    }
  }
  const nodes = steps.map((s) => {
    const { x, y } = pos.get(s.step);
    const label = `${s.step}: ${String(s.status).toLowerCase()}`;
    return svg('g', {
      class: `node st-${s.status}${selected === s.step ? ' sel' : ''}`, transform: `translate(${x} ${y})`,
      tabindex: 0, role: 'button', 'aria-label': label, 'data-step': s.step,
      onclick: () => onSelect(s.step),
      onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(s.step); } },
    },
      svg('rect', { width: NODE_W, height: NODE_H }),
      svg('text', { x: 12, y: 23 }, s.step),
      svg('text', { class: 'st', x: 12, y: 42 },
        `${String(s.status).toLowerCase()}${s.duration_seconds != null ? ' · ' + fmtDuration(s.duration_seconds) : ''}`));
  });
  return h('div', { class: 'dag-wrap' },
    svg('svg', { class: 'dag', width, height, viewBox: `0 0 ${width} ${height}`, role: 'img',
      'aria-label': 'Pipeline steps and their dependencies' },
      svg('defs', {}, svg('marker', { id: 'arrow', viewBox: '0 0 10 10', refX: 9, refY: 5,
        markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' }, svg('path', { d: 'M0 0 L10 5 L0 10 z' }))),
      edges, nodes));
}

export { badge };

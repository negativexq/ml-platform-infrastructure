import { h } from './dom.js';
import { ApiError } from './api.js';
import { projectsView } from './views/projects.js';
import { projectView } from './views/project.js';
import { pipelineRunView } from './views/pipeline_run.js';
import { jobRunView } from './views/job_run.js';
import { modelView } from './views/model.js';
import { deploymentView } from './views/deployment.js';

const root = document.getElementById('view');
const crumbsEl = document.getElementById('crumbs');
const liveEl = document.getElementById('live');
const toastsEl = document.getElementById('toasts');
const dialog = document.getElementById('confirm');

const ROUTES = [
  [/^#\/projects$/, projectsView],
  [/^#\/projects\/([^/]+)$/, projectView],
  [/^#\/projects\/([^/]+)\/pipeline-runs\/([^/]+)$/, pipelineRunView],
  [/^#\/projects\/([^/]+)\/runs\/([^/]+)$/, jobRunView],
  [/^#\/projects\/([^/]+)\/models\/([^/]+)$/, modelView],
  [/^#\/projects\/([^/]+)\/deployments\/([^/]+)$/, deploymentView],
];

let active = null; // the mounted view, so navigation can stop its polling

function toast(message, kind = 'ok') {
  const el = h('div', { class: `toast${kind === 'bad' ? ' bad' : ''}`, role: kind === 'bad' ? 'alert' : 'status' }, message);
  toastsEl.append(el);
  setTimeout(() => el.remove(), kind === 'bad' ? 7000 : 3500);
}

function confirmDialog({ title, body, confirmLabel = 'Confirm', danger = false }) {
  return new Promise((resolve) => {
    dialog.replaceChildren(
      h('h2', {}, title),
      h('div', { class: 'dlg-body' }, body),
      h('div', { class: 'dlg-actions' },
        h('button', { class: 'btn', type: 'button', onclick: () => dialog.close('cancel') }, 'Cancel'),
        h('button', { class: `btn ${danger ? 'danger' : 'primary'}`, type: 'button', 'data-testid': 'confirm-ok',
          onclick: () => dialog.close('ok') }, confirmLabel)));
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'ok'), { once: true });
    dialog.showModal();
  });
}

function setCrumbs(parts) {
  crumbsEl.replaceChildren(...parts.flatMap((p, i) => [
    h('span', { class: 'sep' }),
    p.href ? h('a', { href: p.href }, p.label) : h('span', {}, p.label),
  ]));
}

/** Load -> paint, again every few seconds while `isActive(data)` says something is in flight. */
function mount({ load, paint, isActive = () => false, interval = 3000 }) {
  let stopped = false, timer = null, failures = 0;
  const handle = {
    stop() { stopped = true; clearTimeout(timer); liveEl.classList.remove('on'); },
    async refresh() { clearTimeout(timer); await tick(); },
  };
  async function tick() {
    try {
      const data = await load();
      if (stopped) return;
      failures = 0;
      root.querySelector('.alert.net')?.remove();
      paint(data);
      const again = isActive(data);
      liveEl.classList.toggle('on', again);
      if (again) timer = setTimeout(tick, interval);
    } catch (error) {
      if (stopped) return;
      failures += 1;
      if (error instanceof ApiError && error.status === 404) {
        root.replaceChildren(h('div', { class: 'empty' }, `Not found: ${error.message}`));
        liveEl.classList.remove('on');
        return;
      }
      const banner = h('div', { class: 'alert bad net', role: 'alert' },
        `${error.message || 'Something went wrong'} — retrying…`);
      root.querySelector('.alert.net')?.remove();
      root.prepend(banner);
      timer = setTimeout(tick, Math.min(15000, 3000 * failures));
    }
  }
  tick();
  return handle;
}

/** Run a mutating call, report the outcome, then refresh the page's data. */
async function act(call, successMessage, view) {
  try {
    await call();
    toast(successMessage);
  } catch (error) {
    toast(error.message || 'Action failed', 'bad');
  }
  await view?.refresh();
}

/** Replace the page content. Unlike replaceChildren, null/false children are skipped, not printed. */
function render(...children) {
  root.replaceChildren(...children.flat(Infinity).filter((c) => c != null && c !== false));
}

const ctx = { root, toast, confirm: confirmDialog, setCrumbs, mount, act, render };

async function navigate() {
  active?.stop();
  active = null;
  const hash = location.hash || '#/projects';
  for (const [pattern, view] of ROUTES) {
    const match = hash.match(pattern);
    if (match) {
      root.replaceChildren(h('p', { class: 'muted' }, 'Loading…'));
      active = view(ctx, ...match.slice(1).map(decodeURIComponent));
      root.focus({ preventScroll: true });
      return;
    }
  }
  location.hash = '#/projects';
}

window.addEventListener('hashchange', navigate);
navigate();

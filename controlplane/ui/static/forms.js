import { h } from './dom.js';
import { ApiError } from './api.js';

/**
 * A modal form. `fields`: [{ name, label, type?, required?, pattern?, hint?, options?, value?, placeholder? }].
 * `submit(values)` performs the API call; an ApiError is shown inside the form, so the user can fix and retry.
 * Resolves with whatever `submit` returned, or null if the dialog was cancelled.
 */
export function formDialog(dialog, { title, intro, fields, submitLabel = 'Save', submit }) {
  return new Promise((resolve) => {
    let result = null;
    const errorEl = h('div', { class: 'alert bad form-error', role: 'alert', hidden: true });
    const inputs = new Map();

    const rows = fields.map((f) => {
      const id = `f-${f.name}`;
      const control = f.options
        ? h('select', { id, name: f.name, required: f.required }, f.options.map((o) =>
          h('option', { value: o.value, selected: o.value === f.value }, o.label)))
        : f.type === 'textarea'
          ? h('textarea', { id, name: f.name, rows: 3, placeholder: f.placeholder, value: f.value })
          : h('input', { id, name: f.name, type: f.type || 'text', required: f.required, pattern: f.pattern,
            placeholder: f.placeholder, value: f.value, autocomplete: 'off', spellcheck: 'false' });
      if (f.value != null && !f.options) control.value = f.value;
      inputs.set(f.name, control);
      const hint = f.hint ? h('small', { class: 'hint', id: `${id}-hint` }, f.hint) : null;
      if (hint) control.setAttribute('aria-describedby', `${id}-hint`);
      return h('div', { class: 'field' }, h('label', { for: id }, f.label, f.required ? h('span', { class: 'req', 'aria-hidden': 'true' }, ' *') : null), control, hint);
    });

    const submitBtn = h('button', { class: 'btn primary', type: 'submit', 'data-testid': 'form-submit' }, submitLabel);
    const form = h('form', { method: 'dialog', novalidate: true, class: 'form' },
      h('h2', {}, title), intro ? h('p', { class: 'muted' }, intro) : null, ...rows, errorEl,
      h('div', { class: 'dlg-actions' },
        h('button', { class: 'btn', type: 'button', onclick: () => dialog.close('cancel') }, 'Cancel'), submitBtn));

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      errorEl.hidden = true;
      for (const [name, el] of inputs) { // native validation messages, one at a time, next to the field
        if (!el.checkValidity()) {
          el.setAttribute('aria-invalid', 'true');
          errorEl.textContent = el.validationMessage && el.pattern
            ? `${fields.find((f) => f.name === name).label}: ${fields.find((f) => f.name === name).hint || 'invalid value'}`
            : `${fields.find((f) => f.name === name).label}: ${el.validationMessage || 'invalid value'}`;
          errorEl.hidden = false;
          el.focus();
          return;
        }
        el.removeAttribute('aria-invalid');
      }
      const values = Object.fromEntries([...inputs].map(([name, el]) => [name, el.value.trim()]));
      submitBtn.disabled = true;
      try {
        result = await submit(values);
        dialog.close('ok');
      } catch (error) {
        errorEl.textContent = error instanceof ApiError ? error.message : 'Something went wrong';
        errorEl.hidden = false;
        submitBtn.disabled = false;
      }
    });

    dialog.className = 'wide';
    dialog.replaceChildren(form);
    dialog.addEventListener('close', () => { dialog.className = ''; resolve(dialog.returnValue === 'ok' ? result : null); }, { once: true });
    dialog.showModal();
    inputs.values().next().value?.focus();
  });
}

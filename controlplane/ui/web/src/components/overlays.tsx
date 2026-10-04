import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode,
} from 'react';
import { ApiError } from '../api/client';

/** Native <dialog>, opened modally while `open`. Esc and backdrop handling come from the platform. */
export function Modal({
  open, className = '', label, onClose, children,
}: { open: boolean; className?: string; label?: string; onClose: (returnValue: string) => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (open && !el.open) el.showModal();
    if (!open && el.open) el.close();
  }, [open]);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const handler = () => onClose(el.returnValue);
    el.addEventListener('close', handler);
    return () => el.removeEventListener('close', handler);
  }, [onClose]);
  return <dialog ref={ref} className={className} aria-label={label}>{open ? children : null}</dialog>;
}

// -- toasts, confirm, forms --------------------------------------------------------------

export type ConfirmOptions = { title: string; body: ReactNode; confirmLabel?: string; danger?: boolean };

export type Field = {
  name: string; label: string; type?: 'text' | 'textarea'; required?: boolean; pattern?: string;
  visibleWhen?: (values: Record<string, string>) => boolean;
  hint?: string; placeholder?: string; value?: string; options?: { value: string; label: string }[];
};
export type FormOptions<T> = {
  title: string; intro?: string; fields: Field[]; submitLabel?: string;
  submit: (values: Record<string, string>) => Promise<T>;
  /** What will happen if this is submitted, recomputed as the form changes: for anything that
   * changes traffic or policy, the operator sees the consequence before committing to it. */
  preview?: (values: Record<string, string>) => ReactNode;
};

type Toast = { id: number; message: string; kind: 'ok' | 'bad' };
type Overlays = {
  toast: (message: string, kind?: 'ok' | 'bad') => void;
  confirm: (options: ConfirmOptions) => Promise<boolean>;
  form: <T>(options: FormOptions<T>) => Promise<T | null>;
};

const Ctx = createContext<Overlays | null>(null);
export const useOverlays = () => {
  const value = useContext(Ctx);
  if (!value) throw new Error('OverlayProvider is missing');
  return value;
};

export function OverlayProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [confirmReq, setConfirmReq] = useState<{ options: ConfirmOptions; resolve: (ok: boolean) => void } | null>(null);
  const [formReq, setFormReq] = useState<{ options: FormOptions<unknown>; resolve: (v: unknown) => void } | null>(null);
  const nextId = useRef(1);

  const toast = useCallback((message: string, kind: 'ok' | 'bad' = 'ok') => {
    const id = nextId.current++;
    setToasts((all) => [...all, { id, message, kind }]);
    setTimeout(() => setToasts((all) => all.filter((t) => t.id !== id)), kind === 'bad' ? 7000 : 3500);
  }, []);
  const confirm = useCallback(
    (options: ConfirmOptions) => new Promise<boolean>((resolve) => setConfirmReq({ options, resolve })), []);
  const form = useCallback(
    <T,>(options: FormOptions<T>) =>
      new Promise<T | null>((resolve) =>
        setFormReq({ options: options as FormOptions<unknown>, resolve: resolve as (v: unknown) => void })), []);

  const value = useMemo(() => ({ toast, confirm, form }), [toast, confirm, form]);
  const closeConfirm = useCallback((rv: string) => {
    setConfirmReq((req) => { req?.resolve(rv === 'ok'); return null; });
  }, []);

  return (
    <Ctx.Provider value={value}>
      {children}
      <div id="toasts" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast${t.kind === 'bad' ? ' bad' : ''}`} role={t.kind === 'bad' ? 'alert' : 'status'}>
            {t.message}
          </div>
        ))}
      </div>
      <Modal open={confirmReq !== null} onClose={closeConfirm}>
        {confirmReq && (
          <ConfirmBody options={confirmReq.options} />
        )}
      </Modal>
      <Modal open={formReq !== null} className="wide" onClose={() => setFormReq(null)}>
        {formReq && <FormBody request={formReq} />}
      </Modal>
    </Ctx.Provider>
  );
}

function closeDialog(el: HTMLElement, value: string) {
  (el.closest('dialog') as HTMLDialogElement | null)?.close(value);
}

function ConfirmBody({ options }: { options: ConfirmOptions }) {
  return (
    <>
      <h2>{options.title}</h2>
      <div className="dlg-body">{options.body}</div>
      <div className="dlg-actions">
        <button className="btn" type="button" onClick={(e) => closeDialog(e.currentTarget, 'cancel')}>Cancel</button>
        <button className={`btn ${options.danger ? 'danger' : 'primary'}`} type="button" data-testid="confirm-ok"
          onClick={(e) => closeDialog(e.currentTarget, 'ok')}>
          {options.confirmLabel ?? 'Confirm'}
        </button>
      </div>
    </>
  );
}

function FormBody({ request }: { request: { options: FormOptions<unknown>; resolve: (v: unknown) => void } }) {
  const { options, resolve } = request;
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const refs = useRef(new Map<string, HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>());
  const done = useRef(false);
  const read = () => Object.fromEntries(options.fields.filter((f) => refs.current.has(f.name)).map((f) => [f.name, (refs.current.get(f.name)?.value ?? '').trim()]));
  const [values, setValues] = useState<Record<string, string>>(() => Object.fromEntries(options.fields.map((f) => [f.name, f.value ?? f.options?.[0]?.value ?? ''])));

  useEffect(() => {
    const first = options.fields[0];
    if (first) refs.current.get(first.name)?.focus();
    return () => { if (!done.current) resolve(null); };
  }, [options, resolve]);

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formEl = event.currentTarget; // React clears currentTarget once the handler yields
    setError(null);
    for (const field of options.fields) {
      const el = refs.current.get(field.name);
      if (!el) continue;
      if (!el.checkValidity()) {
        el.setAttribute('aria-invalid', 'true');
        setError(`${field.label}: ${el.getAttribute('pattern') ? field.hint || 'invalid value' : el.validationMessage || 'invalid value'}`);
        el.focus();
        return;
      }
      el.removeAttribute('aria-invalid');
    }
    const submitted = read();
    setBusy(true);
    try {
      const result = await options.submit(submitted);
      done.current = true;
      resolve(result);
      closeDialog(formEl, 'ok');
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Something went wrong');
      setBusy(false);
    }
  }

  const setRef = (name: string) => (el: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement | null) => {
    if (el) refs.current.set(name, el); else refs.current.delete(name);
  };

  return (
    <form className="form" noValidate onSubmit={onSubmit} onInput={() => setValues((old) => ({ ...old, ...read() }))} onChange={() => setValues((old) => ({ ...old, ...read() }))}>
      <h2>{options.title}</h2>
      {options.intro && <p className="muted">{options.intro}</p>}
      {options.fields.filter((f) => !f.visibleWhen || f.visibleWhen(values)).map((f) => {
        const id = `f-${f.name}`;
        const hint = f.hint ? `${id}-hint` : undefined;
        return (
          <div className="field" key={f.name}>
            <label htmlFor={id}>{f.label}{f.required && <span className="req" aria-hidden="true"> *</span>}</label>
            {f.options ? (
              <select id={id} name={f.name} required={f.required} defaultValue={values[f.name]} ref={setRef(f.name)} aria-describedby={hint}>
                {f.options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            ) : f.type === 'textarea' ? (
              <textarea id={id} name={f.name} rows={3} placeholder={f.placeholder} defaultValue={values[f.name]} ref={setRef(f.name)} aria-describedby={hint} />
            ) : (
              <input id={id} name={f.name} type="text" required={f.required} pattern={f.pattern} placeholder={f.placeholder}
                defaultValue={values[f.name]} autoComplete="off" spellCheck={false} ref={setRef(f.name)} aria-describedby={hint} />
            )}
            {f.hint && <small className="hint" id={hint}>{f.hint}</small>}
          </div>
        );
      })}
      {options.preview && (
        <div className="form-preview" data-testid="form-preview" aria-live="polite">
          <h3>What will happen</h3>
          {options.preview(values)}
        </div>)}
      {error && <div className="alert bad form-error" role="alert">{error}</div>}
      <div className="dlg-actions">
        <button className="btn" type="button" onClick={(e) => closeDialog(e.currentTarget, 'cancel')}>Cancel</button>
        <button className="btn primary" type="submit" disabled={busy} data-testid="form-submit">{options.submitLabel ?? 'Save'}</button>
      </div>
    </form>
  );
}

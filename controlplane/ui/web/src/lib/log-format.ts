export type LogLevel = 'DEBUG' | 'INFO' | 'WARN' | 'ERROR' | 'FATAL' | 'LOG';
export type LogEntry = {
  raw: string; n: number; timestamp: string | null; level: LogLevel;
  source: string; message: string; fields: Record<string, unknown>; error: boolean;
  detail: string; repeat: number; header: boolean;
};

const ERROR_LINE = /\b(error|exception|traceback|fatal|failed|killed)\b/i;
const LEVELS: Record<string, LogLevel> = {
  trace: 'DEBUG', debug: 'DEBUG', info: 'INFO', warn: 'WARN', warning: 'WARN',
  error: 'ERROR', fatal: 'FATAL', critical: 'FATAL',
};
const STANDARD = new Set(['timestamp', 'time', 'ts', 'level', 'severity', 'service', 'component', 'logger', 'event', 'message', 'msg']);

function string(value: unknown): string | null { return typeof value === 'string' ? value : null; }
function stamp(value: unknown): string | null {
  const text = string(value);
  return text && Number.isFinite(Date.parse(text)) ? text : null;
}

/** A display schema shared by JSON/structlog, Python-style levels and plain stdout. */
export function parseLog(raw: string, n: number): LogEntry {
  let message = raw;
  let timestamp: string | null = null;
  let source = '';
  let level: LogLevel = 'LOG';
  let fields: Record<string, unknown> = {};
  let header = false;
  // Kubernetes prepends RFC3339Nano when timestamps=true.
  const prefix = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))\s(.*)$/.exec(raw);
  if (prefix) { timestamp = stamp(prefix[1]); message = prefix[2]!; }
  const python = /^(\d{4}[/-]\d{2}[/-]\d{2} \d{2}:\d{2}:\d{2}(?:[,.]\d+)?)\s+(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\s+([^\s:]+):\s*(.*)$/i.exec(message);
  if (python) {
    // No timezone in this legacy format; show the original timestamp without inventing one.
    timestamp ??= python[1]!.replaceAll('/', '-').replace(',', '.');
    level = LEVELS[python[2]!.toLowerCase()] ?? 'LOG'; source = python[3]!; message = python[4]!; header = true;
  }
  try {
    const parsed: unknown = JSON.parse(message);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      const record = parsed as Record<string, unknown>;
      timestamp = stamp(record.timestamp ?? record.time ?? record.ts) ?? timestamp;
      level = LEVELS[(string(record.level ?? record.severity) ?? '').toLowerCase()] ?? 'LOG';
      source = string(record.service ?? record.component ?? record.logger) ?? '';
      message = string(record.event ?? record.message ?? record.msg) ?? message;
      fields = Object.fromEntries(Object.entries(record).filter(([key]) => !STANDARD.has(key)));
      header = true;
    }
  } catch { /* Plain stdout is a supported format. */ }
  // Argo emits logfmt, including error="<nil>" on successful process exits.
  // Its explicit INFO level must win over the presence of the word "error".
  if (!header && /(?:^|\s)level=(?:debug|info|warn|warning|error|fatal)(?:\s|$)/i.test(message)) {
    const record: Record<string, string> = {};
    for (const match of message.matchAll(/(?:^|\s)([\w.]+)=("(?:[^"\\]|\\.)*"|[^\s]+)/g)) {
      const value = match[2]!;
      try { record[match[1]!] = value.startsWith('"') ? JSON.parse(value) as string : value; }
      catch { record[match[1]!] = value; }
    }
    if (record.msg || record.message) {
      timestamp = stamp(record.time ?? record.timestamp) ?? timestamp;
      level = LEVELS[(record.level ?? '').toLowerCase()] ?? 'LOG';
      source = record.logger ?? record.component ?? (record.argo === 'true' ? 'argo' : '');
      message = record.msg ?? record.message!;
      fields = Object.fromEntries(Object.entries(record).filter(([key]) => !STANDARD.has(key)));
      header = true;
    }
  }
  if (level === 'LOG') {
    const prefix = /^(?:\[(TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL|CRITICAL)\]|(TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL|CRITICAL)\b)[:\s-]+(.*)$/i.exec(message);
    if (prefix) { level = LEVELS[(prefix[1] ?? prefix[2])!.toLowerCase()] ?? 'LOG'; message = prefix[3]!; header = true; }
  }
  const error = level === 'ERROR' || level === 'FATAL' || (level === 'LOG' && ERROR_LINE.test(message));
  // Keep plain stdout neutral: an inferred warning must not invent a producer's severity.
  return { raw, n, timestamp, source, level, message, fields, error, detail: message, repeat: 1, header };
}

/** Keep verbose diagnostics behind one concise event; group consecutive repeats. */
export function parseLogs(text: string): LogEntry[] {
  const events: LogEntry[] = [];
  for (const [i, raw] of text.replace(/\n$/, '').split('\n').entries()) {
    const entry = parseLog(raw, i + 1);
    const previous = events.at(-1);
    const traceback = previous?.detail.startsWith('Traceback (most recent call last)');
    const continuation = previous && !entry.header && (
      (previous.header && previous.level !== 'LOG' && !/^\w+=/.test(raw) &&
        (previous.source === 'mlflow.utils.git_utils' || /^\s/.test(raw))) ||
      (traceback && (/^\s/.test(raw) || /^[\w.]+(?:Error|Exception):/.test(raw)))
    );
    if (continuation) {
      previous.raw += '\n' + raw; previous.detail += '\n' + entry.message;
      if (previous.level === 'LOG') previous.error ||= entry.error;
    } else events.push(entry);
  }
  for (const entry of events) {
    if (entry.source === 'mlflow.utils.git_utils' && /Failed to import Git|Bad git executable/.test(entry.detail)) {
      entry.message = 'Git is unavailable; commit metadata could not be recorded.';
    } else if (entry.detail.startsWith('Traceback (most recent call last)')) {
      entry.message = entry.detail.split('\n').findLast((line) => /^[\w.]+(?:Error|Exception):/.test(line)) ?? 'Python exception';
    } else entry.message = entry.detail.split('\n')[0] ?? entry.detail;
    if (entry.message.length > 240) entry.message = entry.message.slice(0, 237) + '…';
  }
  const grouped: LogEntry[] = [];
  for (const entry of events) {
    const previous = grouped.at(-1);
    const sameGitWarning = previous && entry.source === 'mlflow.utils.git_utils' &&
      entry.message === 'Git is unavailable; commit metadata could not be recorded.' && entry.message === previous.message;
    if (previous && (entry.detail === previous.detail || sameGitWarning) && entry.level === previous.level && entry.source === previous.source && JSON.stringify(entry.fields) === JSON.stringify(previous.fields)) {
      if (entry.detail !== previous.detail) previous.detail += '\n\n' + entry.detail;
      previous.repeat++; previous.raw += '\n' + entry.raw;
    } else grouped.push(entry);
  }
  return grouped;
}

export function logTime(timestamp: string | null): string {
  if (!timestamp) return '—';
  const d = new Date(timestamp);
  return `${[d.getHours(), d.getMinutes(), d.getSeconds()].map((v) => String(v).padStart(2, '0')).join(':')}.${String(d.getMilliseconds()).padStart(3, '0')}`;
}

export function fieldText(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value) ?? String(value);
}

// Log parser: normalises six log dialects (key=value, JSON, bracketed, pipe, syslog, cloud runtime) and
// correlates lookup keys with resource results through trace ids.
import fs from 'node:fs';
import { tokenFromValue, tokensFromText } from '../normalize.js';
import type { ObservedState, Observation } from '../types.js';
import { interpretRecord, normSystem, systemFromResource } from './records.js';
import type { ParsedEvidence } from './structured.js';

interface LogLine {
  ts: string;
  service: string;
  trace?: string;
  event: string;
  kv: Record<string, string>;
  raw: string;
}

const KV_RE = /([A-Za-z_][\w.]*)=("((?:[^"\\]|\\.)*)"|[^\s"]+)/g;
const LOOKUP_KEYS = ['lookup_key', 'match_key', 'subject_key', 'scan_key', 'subject_ref', 'correlation_key', 'owner_key', 'lookup_value', 'subject_reference', 'matched_value'];

function parseLine(raw: string): LogLine | null {
  const tsm = raw.match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+(.*)$/);
  if (!tsm) return null;
  const ts = tsm[1];
  let rest = tsm[2];
  const kv: Record<string, string> = {};
  let service = '';
  let trace: string | undefined;
  let event = '';

  const jsonStart = rest.indexOf('{');
  if (rest.startsWith('{') && jsonStart === 0) {
    try {
      const obj = JSON.parse(rest) as Record<string, unknown>;
      for (const [k, v] of Object.entries(obj)) kv[k] = v === null ? 'null' : String(v);
      service = kv.service ?? kv.logger ?? '';
      trace = kv.trace_id;
      event = kv.event ?? '';
      return { ts, service, trace, event, kv, raw };
    } catch {
      /* fall through */
    }
  }
  // cloud runtime: requestId=.. level=.. function=.. message="k=\"v\" ..."
  const msg = rest.match(/message="((?:[^"\\]|\\.)*)"\s*$/);
  if (msg) {
    const head = rest.slice(0, msg.index);
    rest = head + ' ' + msg[1].replace(/\\"/g, '"');
  }
  // bracketed: [LEVEL] [service] [trace] event: k="v"
  let m = rest.match(/^\[(\w+)\]\s+\[([\w-]+)\]\s+\[([\w-]+)\]\s+(\w+):\s*(.*)$/);
  if (m) {
    service = m[2];
    trace = m[3];
    event = m[4];
    rest = m[5];
  }
  // pipe: LEVEL | service | trace | event | k="v"
  if (!m) {
    m = rest.match(/^(\w+)\s*\|\s*([\w-]+)\s*\|\s*([\w-]+)\s*\|\s*(\w+)\s*\|\s*(.*)$/);
    if (m) {
      service = m[2];
      trace = m[3];
      event = m[4];
      rest = m[5];
    }
  }
  // syslog: <pri>1 node service trace - - k="v"
  if (!m) {
    m = rest.match(/^<\d+>\d+\s+\S+\s+([\w-]+)\s+([\w-]+)\s+-\s+-\s+(.*)$/);
    if (m) {
      service = m[1];
      trace = m[2];
      rest = m[3];
    }
  }
  for (const k of rest.matchAll(KV_RE)) kv[k[1]] = k[3] !== undefined ? k[3] : k[2];
  service = kv.service ?? kv.function ?? service;
  trace = kv.trace_id ?? kv.requestId ?? trace;
  event = kv.event ?? event;
  if (trace && !/^(tr|rb)-/.test(trace)) trace = undefined; // instance names are not traces
  return { ts, service, trace, event, kv, raw };
}

function isTrue(v: string | undefined): boolean {
  return v !== undefined && ['true', '1', 'yes', 'allow', 'allowed'].includes(v.toLowerCase());
}

function lineIdentifiers(kv: Record<string, string>): string[] {
  const out = new Set<string>();
  for (const [k, v] of Object.entries(kv)) {
    if (['resource_id', 'trace_id', 'instance', 'pod', 'region', 'requestId', 'archive_key', 'object_key', 'connector_name', 'index', 'file', 'cache', 'pool'].includes(k)) continue;
    const key = LOOKUP_KEYS.includes(k) ? (v.includes('@') ? 'email' : /^\+?[\d\s()-]{7,}$/.test(v) ? 'phone' : k) : k;
    const t = tokenFromValue(key, v);
    if (t) out.add(t);
  }
  return [...out];
}

/** Derive (system, category, state) from a log line that names a resource. */
function interpretLogLine(line: LogLine, resourceId: string): { system: string; category?: string; state?: ObservedState } | null {
  const kv = line.kv;
  const ev = line.event;
  const system = normSystem(kv.system ?? (line.service === 'support_replica' ? 'support_platform' : line.service) ?? systemFromResource(resourceId) ?? '');
  const sys = systemFromResource(resourceId) ?? system;
  switch (ev) {
    case 'profile_lookup_completed': {
      const absent = kv.http_status === '404' || kv.rows === '0';
      return { system: 'account_service', category: 'USER_PROFILE', state: absent ? 'ABSENT' : 'PRESENT' };
    }
    case 'index_query_completed': {
      const hit = (kv.result ?? '').toLowerCase() === 'hit' || Number(kv.total_hits ?? 0) > 0;
      return { system: 'search_service', category: 'SEARCH_INDEX_ENTRY', state: hit ? 'PRESENT' : 'ABSENT' };
    }
    case 'analytics_profile_read':
    case 'analytics_profile_read_completed': {
      const ids = ['email', 'phone', 'email_hash', 'customer_reference'].some((k) => kv[k] && kv[k] !== 'null');
      const direct = kv.direct_identifiers !== undefined ? Number(kv.direct_identifiers) > 0 : ids;
      return { system: 'analytics_platform', category: 'ANALYTICS_PROFILE', state: direct || ids ? 'IDENTIFIABLE' : 'ANONYMIZED' };
    }
    case 'analytics_identity_scan':
      return { system: 'analytics_platform', category: 'ANALYTICS_PROFILE', state: 'IDENTIFIABLE' };
    case 'contact_lookup_completed':
      // marketing-contact style record keyed by a (possibly historical) identifier hash
      return { system: sys, category: 'SECONDARY_IDENTIFIER', state: kv.rows === '0' ? 'ABSENT' : 'PRESENT' };
    case 'restore_candidate_evaluated': {
      const open = isTrue(kv.eligible) && !isTrue(kv.deletion_block);
      return { system: 'backup_service', category: 'BACKUP_ARCHIVE', state: open ? 'RESTORABLE' : 'BLOCKED' };
    }
    case 'restore_policy_read_completed':
      return { system: 'backup_service', category: 'BACKUP_ARCHIVE', state: isTrue(kv.restore_allowed) ? 'RESTORABLE' : 'BLOCKED' };
    case 'restore_authorization_completed':
      return { system: 'backup_service', category: 'BACKUP_ARCHIVE', state: (kv.decision ?? '').toLowerCase() === 'allow' ? 'RESTORABLE' : 'BLOCKED' };
    case 'connector_heartbeat':
    case 'connector_config_read_completed':
    case 'connector_config_loaded':
      return { system: 'integration_service', category: 'DATA_FLOW', state: isTrue(kv.enabled) ? 'ACTIVE' : 'DISABLED' };
    case 'audit_record_read_completed':
      return { system: 'security_service', category: 'SECURITY_AUDIT_LOG', state: kv.rows === '0' ? 'ABSENT' : 'PRESENT' };
    case 'message_pii_scan':
      return { system: 'support_platform', category: 'SUPPORT_MESSAGE', state: 'IDENTIFIABLE' };
    case 'message_read': {
      const t = kv.message_text ?? '';
      const exposed = tokensFromText(t).length > 0 || ['requester_email', 'requester_name', 'requester_phone'].some((k) => kv[k] && kv[k] !== 'null');
      return { system: 'support_platform', category: 'SUPPORT_MESSAGE', state: exposed ? 'IDENTIFIABLE' : 'REDACTED' };
    }
    case 'attachment_pii_scan':
      return { system: 'support_platform', category: 'SUPPORT_ATTACHMENT', state: 'PRESENT' };
    case 'support_ticket_read_completed': {
      const exposed = ['requester_email', 'requester_name', 'requester_phone'].some((k) => kv[k] && kv[k] !== 'null');
      return { system: 'support_platform', category: 'SUPPORT_MESSAGE', state: exposed ? 'IDENTIFIABLE' : 'REDACTED' };
    }
    case 'contact_query_completed':
      // legacy contact index copy of a support contact: no shared conversation context -> whole record
      return { system: 'support_platform', category: 'SUPPORT_CONTACT_RECORD', state: kv.rows === '0' ? 'ABSENT' : 'PRESENT' };
    case 'deletion_marker_check':
      return { system: 'support_platform', category: 'SUPPORT_CONTACT_RECORD', state: isTrue(kv.marker_found) ? 'ABSENT' : 'PRESENT' };
    case 'export_download_check_completed':
    case 'export_metadata_read_completed':
      return { system: 'export_service', category: 'USER_EXPORT', state: kv.available === 'false' ? 'ABSENT' : 'PRESENT' };
    case 'retained_order_loaded':
      return { system: 'legal_service', category: 'LEGAL_HOLD_RECORD', state: 'PRESENT' };
    case 'record_lookup':
      return null; // handled by the structured record path
    default:
      return null;
  }
}

export function parseLogFile(fileId: string, filePath: string, capturedAt: string): ParsedEvidence {
  const text = fs.readFileSync(filePath, 'utf8');
  const lines = text
    .split(/\r?\n/)
    .map(parseLine)
    .filter((l): l is LogLine => !!l);
  const traceKeys = new Map<string, Set<string>>();
  for (const l of lines) {
    if (!l.trace) continue;
    const set = traceKeys.get(l.trace) ?? new Set<string>();
    for (const t of lineIdentifiers(Object.fromEntries(Object.entries(l.kv).filter(([k]) => LOOKUP_KEYS.includes(k))))) set.add(t);
    traceKeys.set(l.trace, set);
  }
  const observations: Observation[] = [];
  for (const l of lines) {
    const rid = l.kv.resource_id;
    if (!rid) continue;
    if (l.event === 'record_lookup' || (l.kv.record_type && l.kv.result)) {
      const obs = interpretRecord(
        { evidenceId: fileId, kind: 'file', capturedAt, observedAt: l.ts, parser: 'log-record' },
        l.kv.system ?? '',
        'record_lookup',
        { ...l.kv, recorded_at: l.ts },
      );
      if (obs) observations.push(obs);
      continue;
    }
    const interp = interpretLogLine(l, rid);
    const ids = new Set<string>(lineIdentifiers(l.kv));
    if (l.trace) for (const t of traceKeys.get(l.trace) ?? []) ids.add(t);
    if (!interp) {
      // started lines that name the resource still link identity (state unknown)
      if (ids.size) {
        observations.push({
          evidenceId: fileId,
          kind: 'file',
          capturedAt,
          observedAt: l.ts,
          system: normSystem(systemFromResource(rid) ?? l.service),
          resourceId: rid,
          identifiers: [...ids],
          parser: 'log-link',
          note: l.event,
        });
      }
      continue;
    }
    observations.push({
      evidenceId: fileId,
      kind: 'file',
      capturedAt,
      observedAt: l.ts,
      system: interp.system,
      resourceId: rid,
      category: interp.category,
      state: interp.state,
      identifiers: [...ids],
      parser: 'log',
      note: l.event,
    });
  }
  return { observations, scopes: [] };
}

// Interprets a single structured record (from SQL, JSON bundles, exports, ...) into an Observation.
import { DIRECT_ID_KEYS, isNullish, tokenFromValue, tokensFromText } from '../normalize.js';
import type { ObservedState, Observation } from '../types.js';

export type RecordBase = Pick<Observation, 'evidenceId' | 'kind' | 'capturedAt' | 'observedAt' | 'parser'> & { copy?: string };

const SYSTEM_ALIASES: Record<string, string> = {
  profile_service: 'account_service',
  account_failover_replica: 'account_service',
  account_replication_cluster: 'account_service',
  search_index_service: 'search_service',
  object_store: 'object_storage',
};

export function normSystem(system: string): string {
  return SYSTEM_ALIASES[system] ?? system;
}

/** Infer a system from a resource id prefix (used when evidence does not name the system). */
export function systemFromResource(resourceId: string): string | undefined {
  const p = resourceId.replace(/_\d+$/, '');
  switch (p) {
    case 'account':
      return 'account_service';
    case 'search':
      return 'search_service';
    case 'support':
      return 'support_platform';
    case 'analytics':
      return 'analytics_platform';
    case 'export':
      return 'export_service';
    case 'backup':
      return 'backup_service';
    case 'data_flow':
      return 'integration_service';
    case 'tombstone':
      return 'identity_service';
    case 'audit_record':
      return 'security_service';
    case 'marketing':
      return 'marketing_platform';
    case 'legal_hold':
      return 'legal_service';
    case 'object':
      return 'object_storage';
    case 'cache':
      return 'cache_service';
    case 'email_delivery':
      return 'email_delivery_service';
    default:
      return undefined;
  }
}

export const RESOURCE_ID_RE = /\b(?:account|search|support|analytics|export|backup|data_flow|tombstone|audit_record|marketing|legal_hold|object|cache|email_delivery)_\d+\b/g;

/** Map record-lookup style results (FOUND, NOT_FOUND, ...) into observed states. */
export function stateFromResult(result: string, recordType: string): ObservedState | undefined {
  const r = result.toUpperCase();
  const t = recordType.toUpperCase();
  switch (r) {
    case 'FOUND':
    case 'PRESENT':
    case 'RETAINED':
      return 'PRESENT';
    case 'NOT_FOUND':
    case 'ABSENT':
    case 'DELETED':
      return 'ABSENT';
    case 'IDENTIFIABLE':
    case 'EXPOSED':
      return 'IDENTIFIABLE';
    case 'ANONYMIZED':
      return 'ANONYMIZED';
    case 'REDACTED':
      return 'REDACTED';
    case 'ACTIVE':
    case 'ENABLED':
      return t === 'BACKUP_ARCHIVE' ? 'RESTORABLE' : 'ACTIVE';
    case 'DISABLED':
    case 'STOPPED':
      return 'DISABLED';
    case 'BLOCKED':
      return 'BLOCKED';
    case 'RESTORABLE':
      return 'RESTORABLE';
    default:
      return undefined;
  }
}

function collectTokens(rec: Record<string, unknown>, skip: string[] = []): string[] {
  const out = new Set<string>();
  const walk = (obj: Record<string, unknown>) => {
    for (const [k, v] of Object.entries(obj)) {
      if (skip.includes(k)) continue;
      if (v && typeof v === 'object' && !Array.isArray(v)) {
        walk(v as Record<string, unknown>);
        continue;
      }
      if (Array.isArray(v)) continue;
      if (k === 'resource_id') continue;
      const t = tokenFromValue(k, v);
      if (t) out.add(t);
      if (typeof v === 'string' && v.length > 30) for (const x of tokensFromText(v)) out.add(x);
    }
  };
  walk(rec);
  return [...out];
}

function hasDirectIds(rec: Record<string, unknown>, extraKeys: string[] = []): boolean {
  const keys = [...DIRECT_ID_KEYS, ...extraKeys];
  const walk = (obj: Record<string, unknown>): boolean => {
    for (const [k, v] of Object.entries(obj)) {
      if (v && typeof v === 'object' && !Array.isArray(v)) {
        if (walk(v as Record<string, unknown>)) return true;
        continue;
      }
      if (keys.includes(k) && !isNullish(v)) return true;
    }
    return false;
  };
  return walk(rec);
}

function truthy(v: unknown): boolean {
  if (typeof v === 'boolean') return v;
  if (typeof v === 'number') return v !== 0;
  if (typeof v === 'string') return ['1', 'true', 'yes', 'y', 'enabled', 'allow', 'allowed'].includes(v.toLowerCase());
  return false;
}

/**
 * Interpret a structured record. `hint` is a collection / table name that tells us which kind of record it is.
 */
export function interpretRecord(
  base: RecordBase,
  systemIn: string,
  hint: string,
  rec: Record<string, unknown>,
): Observation | null {
  // Record-lookup / operational-history style rows (system, resource_id, record_type, result, subject)
  if (typeof rec.record_type === 'string' && typeof rec.result === 'string' && typeof rec.resource_id === 'string') {
    const system = normSystem(String(rec.system ?? systemIn ?? systemFromResource(rec.resource_id) ?? 'unknown'));
    const state = stateFromResult(rec.result, rec.record_type);
    const ids = collectTokens(rec, ['system', 'record_type', 'result', 'recorded_at']);
    const subj = String(rec.subject_reference ?? rec.lookup_value ?? '');
    if (/^no direct identifier$/i.test(subj)) {
      // explicit statement that nothing identifies the subject any more
    }
    return {
      ...base,
      observedAt: String(rec.recorded_at ?? base.observedAt),
      system,
      resourceId: rec.resource_id,
      category: rec.record_type === 'DATA_FLOW' ? undefined : rec.record_type,
      state,
      identifiers: ids,
    };
  }

  const resourceId = String(rec.resource_id ?? '');
  if (!resourceId) return null;
  const system = normSystem(systemIn || systemFromResource(resourceId) || 'unknown');
  const h = hint.toLowerCase();
  const observedAt = String(rec.observed_at ?? rec.updated_at ?? base.observedAt);
  const ids = collectTokens(rec, ['archive_key', 'archive_path', 'object_key', 'integrity_seal', 'file_name']);
  if (typeof rec.source_resource_id === 'string') ids.push(`res:${rec.source_resource_id}`);
  const mk = (state: ObservedState | undefined, category?: string, extra: Partial<Observation> = {}): Observation => ({
    ...base,
    observedAt,
    system,
    resourceId,
    category,
    state,
    identifiers: [...new Set(ids)],
    ...extra,
  });

  switch (system) {
    case 'account_service': {
      if (h.includes('profile_lookup')) {
        const code = Number(rec.status_code ?? 0);
        const prof = rec.profile as Record<string, unknown> | null;
        return mk(code === 404 || !prof ? 'ABSENT' : 'PRESENT', 'USER_PROFILE');
      }
      const status = String(rec.account_status ?? '').toUpperCase();
      return mk(status === 'DELETED' || status === 'PURGED' ? 'ABSENT' : 'PRESENT', 'USER_PROFILE');
    }
    case 'search_service': {
      if (h.includes('index_quer')) {
        const hitsObj = rec.hits as { total?: number; documents?: Record<string, unknown>[] } | number | null;
        const total = typeof hitsObj === 'number' ? hitsObj : Number(hitsObj?.total ?? hitsObj?.documents?.length ?? 0);
        const docs = typeof hitsObj === 'object' && hitsObj ? hitsObj.documents ?? [] : [];
        const own = docs.filter((d) => !d.resource_id || d.resource_id === resourceId);
        for (const d of own) ids.push(...collectTokens(d));
        return mk(total > 0 && (own.length > 0 || docs.length === 0) ? 'PRESENT' : 'ABSENT', 'SEARCH_INDEX_ENTRY');
      }
      return mk('PRESENT', 'SEARCH_INDEX_ENTRY');
    }
    case 'analytics_platform': {
      const attributable = hasDirectIds(rec, ['user_id', 'customer_reference']);
      return mk(attributable ? 'IDENTIFIABLE' : 'ANONYMIZED', 'ANALYTICS_PROFILE');
    }
    case 'support_platform': {
      if (h.includes('attachment') || typeof rec.file_name === 'string' || rec.storage_class === 'ticket_attachment') {
        return mk('PRESENT', 'SUPPORT_ATTACHMENT');
      }
      const text = String(rec.message_text ?? rec.message_excerpt ?? '');
      const textHasPii = tokensFromText(text).some((t) => t.startsWith('email:'));
      const exposed = hasDirectIds(rec) || textHasPii;
      return mk(exposed ? 'IDENTIFIABLE' : 'REDACTED', 'SUPPORT_MESSAGE');
    }
    case 'export_service': {
      if (rec.download_available === false && typeof rec.expires_at === 'string') {
        // expired / unavailable package: the job metadata remains but the file can no longer be downloaded
        return mk('PRESENT', 'USER_EXPORT', { note: 'download_unavailable' });
      }
      return mk('PRESENT', 'USER_EXPORT');
    }
    case 'integration_service': {
      return mk(truthy(rec.enabled) ? 'ACTIVE' : 'DISABLED', 'DATA_FLOW');
    }
    case 'backup_service': {
      return mk(truthy(rec.restore_allowed) ? 'RESTORABLE' : 'BLOCKED', 'BACKUP_ARCHIVE');
    }
    case 'security_service': {
      return mk('PRESENT', 'SECURITY_AUDIT_LOG', { note: rec.retention_until ? `retention_until=${rec.retention_until}` : undefined });
    }
    case 'identity_service': {
      const exposed = hasDirectIds(rec);
      return mk(exposed ? 'IDENTIFIABLE' : 'REDACTED', 'ACCOUNT_TOMBSTONE');
    }
    case 'legal_service': {
      return mk('PRESENT', 'LEGAL_HOLD_RECORD', { note: rec.retained_until ? `retained_until=${rec.retained_until}` : undefined });
    }
    case 'marketing_platform':
      return mk('PRESENT', 'MARKETING_PROFILE');
    case 'object_storage':
      return mk('PRESENT', 'SUPPORT_ATTACHMENT');
    case 'cache_service':
      return mk('PRESENT', 'CACHE_ENTRY');
    case 'email_delivery_service':
      return mk(hasDirectIds(rec, ['recipient', 'to']) ? 'IDENTIFIABLE' : 'REDACTED', 'EMAIL_DELIVERY_LOG');
    default:
      return mk('PRESENT');
  }
}

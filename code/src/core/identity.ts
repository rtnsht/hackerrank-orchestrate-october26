// Identity resolution: current snapshot + historical audit log -> every identifier the subject ever used.
import { foldName, iso, normEmail, normPhone } from './normalize.js';
import type { AuditRow, IdentityRow } from './types.js';

export interface IdentityProfile {
  userId: string;
  name: string;
  currentEmail: string;
  emails: string[];
  phones: string[];
  hashes: string[];
  tokens: Set<string>;
  /** audit_ids that prove historical identifiers (for explainability) */
  historyAuditIds: string[];
  matchedBy: 'current_email' | 'historical_email' | 'none';
}

export class IdentityResolver {
  private byUser = new Map<string, IdentityRow>();
  private auditByUser = new Map<string, AuditRow[]>();
  private emailOwners = new Map<string, { userId: string; from?: string; to?: string }[]>();

  constructor(identities: IdentityRow[], audit: AuditRow[]) {
    for (const r of identities) {
      this.byUser.set(r.user_id, r);
      this.addOwner(r.email, r.user_id);
    }
    for (const a of audit) {
      const list = this.auditByUser.get(a.user_id) ?? [];
      list.push(a);
      this.auditByUser.set(a.user_id, list);
      if (a.field_name === 'email') {
        this.addOwner(a.old_value, a.user_id, undefined, a.event_time);
        this.addOwner(a.new_value, a.user_id, a.event_time);
      }
    }
  }

  private addOwner(email: string, userId: string, from?: string, to?: string) {
    if (!email) return;
    const e = normEmail(email);
    const list = this.emailOwners.get(e) ?? [];
    list.push({ userId, from, to });
    this.emailOwners.set(e, list);
  }

  /** Resolve the user that held `email` at time `at` (current or historical identifier). */
  resolveUser(email: string, at: string): { userId: string; matchedBy: IdentityProfile['matchedBy'] } | null {
    const owners = this.emailOwners.get(normEmail(email)) ?? [];
    if (owners.length === 0) return null;
    const t = iso(at);
    const active = owners.filter((o) => (!o.from || iso(o.from) <= t) && (!o.to || iso(o.to) >= t));
    const pick = (active.length ? active : owners)[0];
    const cur = this.byUser.get(pick.userId);
    const matchedBy = cur && normEmail(cur.email) === normEmail(email) ? 'current_email' : 'historical_email';
    return { userId: pick.userId, matchedBy };
  }

  profile(userId: string, matchedBy: IdentityProfile['matchedBy'] = 'none'): IdentityProfile {
    const cur = this.byUser.get(userId);
    const emails = new Set<string>();
    const phones = new Set<string>();
    const hashes = new Set<string>();
    const names = new Set<string>();
    const historyAuditIds: string[] = [];
    if (cur) {
      if (cur.email) emails.add(normEmail(cur.email));
      if (cur.phone) phones.add(normPhone(cur.phone));
      if (cur.email_hash) hashes.add(cur.email_hash.toLowerCase());
      if (cur.name) names.add(foldName(cur.name));
    }
    for (const a of this.auditByUser.get(userId) ?? []) {
      historyAuditIds.push(a.audit_id);
      for (const v of [a.old_value, a.new_value]) {
        if (!v) continue;
        if (a.field_name === 'email') emails.add(normEmail(v));
        else if (a.field_name === 'phone') phones.add(normPhone(v));
        else if (a.field_name === 'email_hash') hashes.add(v.toLowerCase());
        else if (a.field_name === 'name') names.add(foldName(v));
      }
    }
    const tokens = new Set<string>([`user:${userId}`]);
    for (const e of emails) tokens.add(`email:${e}`);
    for (const p of phones) if (p.length >= 7) tokens.add(`phone:${p}`);
    for (const h of hashes) tokens.add(`hash:${h}`);
    return {
      userId,
      name: cur?.name ?? '',
      currentEmail: cur ? normEmail(cur.email) : '',
      emails: [...emails],
      phones: [...phones],
      hashes: [...hashes],
      tokens,
      historyAuditIds,
      matchedBy,
    };
  }

  /** Every user id that owns a given identifier token (used to detect resources that belong to someone else). */
  ownersOfToken(token: string): string[] {
    const [kind, value] = [token.slice(0, token.indexOf(':')), token.slice(token.indexOf(':') + 1)];
    const out = new Set<string>();
    if (kind === 'user') out.add(value);
    if (kind === 'email') for (const o of this.emailOwners.get(value) ?? []) out.add(o.userId);
    if (kind === 'phone' || kind === 'hash') {
      for (const [uid, row] of this.byUser) {
        if (kind === 'phone' && normPhone(row.phone) === value) out.add(uid);
        if (kind === 'hash' && row.email_hash.toLowerCase() === value) out.add(uid);
      }
      for (const [uid, rows] of this.auditByUser) {
        for (const a of rows) {
          if (kind === 'phone' && a.field_name === 'phone' && (normPhone(a.old_value) === value || normPhone(a.new_value) === value)) out.add(uid);
          if (kind === 'hash' && a.field_name === 'email_hash' && (a.old_value.toLowerCase() === value || a.new_value.toLowerCase() === value)) out.add(uid);
        }
      }
    }
    return [...out];
  }
}

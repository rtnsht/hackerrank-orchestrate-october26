// Identifier normalisation helpers shared by every evidence parser.

const EMAIL_RE = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g;
const USER_RE = /\buser_\d+\b/g;
const HASH_RE = /\b[0-9a-f]{64}\b/gi;

export function normEmail(v: string): string {
  return v.trim().toLowerCase();
}

export function normPhone(v: string): string {
  return v.replace(/\D+/g, '');
}

export function foldName(v: string): string {
  return v
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .replace(/\s+/g, ' ')
    .trim();
}

export function isNullish(v: unknown): boolean {
  if (v === null || v === undefined) return true;
  if (typeof v === 'string') {
    const s = v.trim().toLowerCase();
    return s === '' || s === 'null' || s === 'none' || s === '[redacted]' || s === 'redacted' || s === '-' || s === '—';
  }
  return false;
}

/** Extract every identifier token that appears in free text. */
export function tokensFromText(text: string): string[] {
  const out = new Set<string>();
  for (const m of text.match(EMAIL_RE) ?? []) out.add(`email:${normEmail(m)}`);
  for (const m of text.match(USER_RE) ?? []) out.add(`user:${m}`);
  for (const m of text.match(HASH_RE) ?? []) out.add(`hash:${m.toLowerCase()}`);
  return [...out];
}

/** Classify a single scalar value into an identifier token (or null). */
export function tokenFromValue(key: string, value: unknown): string | null {
  if (isNullish(value) || typeof value === 'object') return null;
  const s = String(value).trim();
  const k = key.toLowerCase();
  if (/^user_\d+$/.test(s)) return `user:${s}`;
  if (/^[^\s@]+@[^\s@]+\.[a-z]{2,}$/i.test(s)) return `email:${normEmail(s)}`;
  if (/^[0-9a-f]{64}$/i.test(s)) return `hash:${s.toLowerCase()}`;
  if (k.includes('phone') && normPhone(s).length >= 7) return `phone:${normPhone(s)}`;
  if ((k.includes('name') && !k.includes('file') && !k.includes('connector') && !k.includes('index') && !k.includes('event')) && /[a-z]/i.test(s)) {
    return `name:${foldName(s)}`;
  }
  return null;
}

/** Direct identifier keys (per policy, these must not remain on tombstones / redacted / anonymised records). */
export const DIRECT_ID_KEYS = [
  'email',
  'phone',
  'email_hash',
  'name',
  'full_name',
  'display_name',
  'requester_name',
  'requester_email',
  'requester_phone',
  'owner_email',
  'owner_phone',
  'customer_email',
];

export function iso(t: string): number {
  const v = Date.parse(t);
  return Number.isNaN(v) ? 0 : v;
}

// Multimodal evidence: PDF text extraction and screenshot OCR, followed by screen-aware interpretation.
// OCR / PDF text is cached on disk (keyed by file content hash) so repeated runs are fast and deterministic.
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { tokensFromText, normPhone } from '../normalize.js';
import type { Observation, ObservedState } from '../types.js';
import { RESOURCE_ID_RE, systemFromResource } from './records.js';
import type { ParsedEvidence } from './structured.js';

export interface CatalogLink {
  activityId: string;
  at: string;
  system: string;
  resourceId: string;
  alias: string;
  email?: string;
  userId?: string;
  exclusive: boolean;
}

/** Which systems a screen / document from a given origin is authoritative about. */
const ORIGIN_SYSTEMS: Record<string, string[]> = {
  account_admin_portal: ['account_service'],
  analytics_platform: ['analytics_platform'],
  backup_service: ['backup_service'],
  integration_admin: ['integration_service'],
  integration_management_service: ['integration_service'],
  export_service: ['export_service'],
  identity_service: ['identity_service'],
  marketing_platform: ['marketing_platform'],
  object_storage: ['object_storage'],
  search_index_admin: ['search_service'],
  security_service: ['security_service'],
  support_platform: ['support_platform'],
  account_history_service: ['export_service', 'account_service'],
  legal_retention_service: ['legal_service'],
};

export interface MediaTextCache {
  [key: string]: { file: string; sha256: string; method: string; text: string };
}

export function sha256File(p: string): string {
  return crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');
}

export async function extractPdfText(filePath: string): Promise<string> {
  const { PDFParse } = await import('pdf-parse');
  const parser = new PDFParse({ data: fs.readFileSync(filePath) });
  try {
    const r = await parser.getText();
    return r.text;
  } finally {
    await parser.destroy();
  }
}

let ocrWorker: Promise<import('tesseract.js').Worker> | null = null;
export async function ocrImage(filePath: string, codeRoot: string): Promise<string> {
  const { createWorker } = await import('tesseract.js');
  const sharp = (await import('sharp')).default;
  if (!ocrWorker) {
    const langPath = path.join(codeRoot, 'node_modules', '@tesseract.js-data', 'eng', '4.0.0_best_int');
    ocrWorker = createWorker('eng', 1, { langPath, cacheMethod: 'none', gzip: true });
  }
  const worker = await ocrWorker;
  // Upscaling + grayscale + sharpen roughly doubles OCR accuracy on dense admin-console screenshots.
  const buf = await sharp(filePath).grayscale().resize({ width: 3600 }).sharpen().toBuffer();
  const { data } = await worker.recognize(buf);
  return data.text;
}

export async function shutdownOcr(): Promise<void> {
  if (ocrWorker) {
    const w = await ocrWorker;
    await w.terminate();
    ocrWorker = null;
  }
}

function linesContaining(lines: string[], needle: string): string[] {
  const n = needle.toLowerCase();
  return lines.filter((l) => l.toLowerCase().includes(n));
}

/** Screen-aware interpretation of OCR / PDF text. */
export function interpretMediaText(
  fileId: string,
  origin: string,
  capturedAt: string,
  text: string,
  catalog: CatalogLink[],
  kind: 'image' | 'pdf',
): ParsedEvidence {
  // OCR frequently renders "user_17" as "user 17"; restore the identifier shape before matching.
  text = text.replace(
    /\b(account|search|support|analytics|export|backup|data_flow|data flow|tombstone|audit_record|audit record|marketing|legal_hold|object|cache|email_delivery|user)[ ](\d{1,6})\b/g,
    (_m, p: string, n: string) => `${p.replace(' ', '_')}_${n}`,
  );
  const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  const found = new Map<string, string[]>(); // resource -> anchor strings
  for (const m of text.matchAll(RESOURCE_ID_RE)) {
    const rid = m[0];
    if (!found.has(rid)) found.set(rid, [rid]);
  }
  for (const link of catalog) {
    if (link.alias && link.alias !== link.resourceId && text.toLowerCase().includes(link.alias.toLowerCase())) {
      const anchors = found.get(link.resourceId) ?? [];
      anchors.push(link.alias);
      found.set(link.resourceId, anchors);
    }
  }
  const authoritative = ORIGIN_SYSTEMS[origin] ?? [];
  const pageTokens = tokensFromText(text);
  // phone numbers are formatted inconsistently; capture "+<digits and separators>" runs
  for (const m of text.matchAll(/\+\d[\d\s().-]{6,}\d/g)) pageTokens.push(`phone:${normPhone(m[0])}`);
  const observations: Observation[] = [];
  const subjectResources = [...found.keys()].filter((r) => authoritative.includes(systemFromResource(r) ?? ''));

  for (const [rid, anchors] of found) {
    const system = systemFromResource(rid) ?? 'unknown';
    const anchorLines = anchors.flatMap((a) => linesContaining(lines, a));
    const lineText = anchorLines.join('\n');
    const singleSubject = subjectResources.length === 1 && subjectResources[0] === rid;
    // Row-level OCR association is unreliable on dense list screens, so identity comes from the data-catalog
    // links (applied later by the linker) or, for single-subject detail screens, from the whole page.
    const ids = new Set<string>();
    if (singleSubject) for (const t of pageTokens) ids.add(t);
    const isSubject = authoritative.includes(system);
    let state: ObservedState | undefined;
    let category: string | undefined;
    if (isSubject) {
      const hasDirect = [...ids].some((t) => t.startsWith('email:') || t.startsWith('phone:'));
      const ctx = (singleSubject ? text : lineText).toLowerCase();
      switch (system) {
        case 'account_service':
          state = /not found|deleted|no record/.test(lineText.toLowerCase()) ? 'ABSENT' : 'PRESENT';
          category = 'USER_PROFILE';
          break;
        case 'analytics_platform':
          state = hasDirect ? 'IDENTIFIABLE' : 'ANONYMIZED';
          category = 'ANALYTICS_PROFILE';
          break;
        case 'backup_service':
          state = /restore (blocked|disabled|denied)|deletion block|blocked/.test(lineText.toLowerCase()) ? 'BLOCKED' : 'RESTORABLE';
          category = 'BACKUP_ARCHIVE';
          break;
        case 'integration_service':
          state = /disabled|paused|stopped|inactive/.test(lineText.toLowerCase()) ? 'DISABLED' : 'ACTIVE';
          category = 'DATA_FLOW';
          break;
        case 'export_service':
          state = /expired|deleted|purged|removed/.test(lineText.toLowerCase()) ? 'ABSENT' : 'PRESENT';
          category = 'USER_EXPORT';
          break;
        case 'identity_service':
          state = hasDirect ? 'IDENTIFIABLE' : 'REDACTED';
          category = 'ACCOUNT_TOMBSTONE';
          break;
        case 'security_service':
          state = 'PRESENT';
          category = 'SECURITY_AUDIT_LOG';
          break;
        case 'search_service':
          state = 'PRESENT';
          category = 'SEARCH_INDEX_ENTRY';
          break;
        case 'support_platform':
          if (kind === 'pdf' || /attachment|\.pdf|upload/.test(ctx)) {
            state = 'PRESENT';
            category = 'SUPPORT_ATTACHMENT';
          } else {
            state = hasDirect ? 'IDENTIFIABLE' : 'REDACTED';
            category = 'SUPPORT_MESSAGE';
          }
          break;
        case 'marketing_platform':
          state = 'PRESENT';
          category = 'MARKETING_PROFILE';
          break;
        case 'object_storage':
          state = 'PRESENT';
          category = 'SUPPORT_ATTACHMENT';
          break;
        case 'legal_service':
          state = 'PRESENT';
          category = 'LEGAL_HOLD_RECORD';
          break;
        default:
          state = 'PRESENT';
      }
    }
    observations.push({
      evidenceId: fileId,
      kind: 'file',
      capturedAt,
      observedAt: capturedAt,
      system,
      resourceId: rid,
      category,
      state,
      identifiers: [...ids],
      parser: kind === 'pdf' ? 'pdf' : 'ocr',
      note: isSubject ? 'screen-subject' : 'screen-reference',
    });
  }
  return { observations, scopes: [], text };
}

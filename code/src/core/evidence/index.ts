// Builds the global evidence index: every participant-visible file and activity, normalised into observations.
import fs from 'node:fs';
import path from 'node:path';
import type { ActivityRow, ManifestRow, Observation, SnapshotScope } from '../types.js';
import { parseLogFile } from './logs.js';
import { extractPdfText, interpretMediaText, ocrImage, sha256File, shutdownOcr, type CatalogLink, type MediaTextCache } from './media.js';
import { normSystem, systemFromResource } from './records.js';
import { parseJsonFile, parseSqlFile, parseTextFile, type ParsedEvidence } from './structured.js';
import { normEmail } from '../normalize.js';

export interface EvidenceIndex {
  observations: Observation[];
  scopes: SnapshotScope[];
  catalog: CatalogLink[];
  byResource: Map<string, Observation[]>;
  manifest: Map<string, ManifestRow>;
  mediaText: Map<string, string>;
  stats: Record<string, number>;
}

export interface MediaExtractor {
  /** Optional LLM (Mastra agent) extraction for screenshots / PDFs. Returns null to fall back to OCR rules. */
  (input: { fileId: string; filePath: string; origin: string; capturedAt: string; text: string }): Promise<ParsedEvidence | null>;
}

export function parseCatalogLinks(activity: ActivityRow[]): CatalogLink[] {
  const out: CatalogLink[] = [];
  for (const a of activity) {
    if (a.event_type !== 'LINK_RESOURCE_IDENTITY') continue;
    const m = a.details.match(/\b([\w]+_\d+)\s*\(([^)]+)\)/);
    const email = a.details.match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/);
    const user = a.details.match(/\buser_\d+\b(?![^(]*\))/g);
    out.push({
      activityId: a.activity_id,
      at: a.event_time,
      system: normSystem(a.target_system),
      resourceId: a.resource_id,
      alias: m ? m[2].trim() : a.resource_id,
      email: email ? normEmail(email[0]) : undefined,
      userId: user ? user[user.length - 1] : undefined,
      exclusive: /belongs only to|no other subject/i.test(a.details),
    });
  }
  return out;
}

/** Activities are workflow claims; only explicit checks / restores are treated as state observations. */
export function activityObservations(activity: ActivityRow[], catalog: CatalogLink[]): Observation[] {
  const out: Observation[] = [];
  for (const a of activity) {
    const system = normSystem(a.target_system);
    const base = { evidenceId: a.activity_id, kind: 'activity' as const, capturedAt: a.event_time, observedAt: a.event_time, parser: 'activity' };
    switch (a.event_type) {
      case 'CHECK_DATA_FLOW': {
        const enabled = /remains enabled|still enabled|is enabled|active/i.test(a.details) && !/disabled/i.test(a.details);
        out.push({ ...base, system, resourceId: a.resource_id, category: 'DATA_FLOW', state: enabled ? 'ACTIVE' : 'DISABLED', identifiers: [], note: a.details });
        break;
      }
      case 'CHECK_RESTORE_ACCESS': {
        const open = /can still be restored|restorable|restore (is )?allowed/i.test(a.details);
        out.push({ ...base, system, resourceId: a.resource_id, category: 'BACKUP_ARCHIVE', state: open ? 'RESTORABLE' : 'BLOCKED', identifiers: [], note: a.details });
        break;
      }
      case 'RESTORE_BACKUP': {
        const m = a.details.match(/resource\s+([\w]+_\d+)/);
        if (m && a.reported_result === 'SUCCESS') {
          const rid = m[1];
          out.push({ ...base, system: systemFromResource(rid) ?? system, resourceId: rid, state: 'PRESENT', identifiers: [], note: `recreated by restore of ${a.resource_id}` });
          out.push({ ...base, system: 'backup_service', resourceId: a.resource_id, category: 'BACKUP_ARCHIVE', state: 'RESTORABLE', identifiers: [], note: a.details });
        }
        break;
      }
      case 'LINK_RESOURCE_IDENTITY': {
        const link = catalog.find((c) => c.activityId === a.activity_id);
        if (link) {
          const ids = [link.email ? `email:${link.email}` : '', link.userId ? `user:${link.userId}` : ''].filter(Boolean);
          out.push({ ...base, system, resourceId: a.resource_id, identifiers: ids, note: 'catalog-link' });
        }
        break;
      }
      default:
        // every other activity is a claim (deletion worker results, sync, indexing...) - recorded but never proof
        out.push({ ...base, system, resourceId: a.resource_id, identifiers: [], claim: true, note: `${a.event_type}:${a.reported_result}` });
    }
  }
  return out;
}

export async function buildEvidenceIndex(opts: {
  datasetDir: string;
  codeRoot: string;
  manifest: ManifestRow[];
  activity: ActivityRow[];
  mediaExtractor?: MediaExtractor;
  cachePath?: string;
  log?: (msg: string) => void;
}): Promise<EvidenceIndex> {
  const log = opts.log ?? (() => {});
  const catalog = parseCatalogLinks(opts.activity);
  const observations: Observation[] = [];
  const scopes: SnapshotScope[] = [];
  const mediaText = new Map<string, string>();
  const stats: Record<string, number> = { files: 0, sql: 0, json: 0, log: 0, text: 0, pdf: 0, image: 0, ocrCalls: 0, llmMediaCalls: 0 };
  const cachePath = opts.cachePath ?? path.join(opts.codeRoot, 'cache', 'media_text_cache.json');
  let cache: MediaTextCache = {};
  if (fs.existsSync(cachePath)) cache = JSON.parse(fs.readFileSync(cachePath, 'utf8'));
  let cacheDirty = false;

  for (const row of opts.manifest) {
    const filePath = path.join(opts.datasetDir, row.file_path);
    if (!fs.existsSync(filePath)) {
      log(`missing evidence file ${row.file_path}`);
      continue;
    }
    stats.files++;
    const ext = path.extname(filePath).toLowerCase();
    let parsed: ParsedEvidence = { observations: [], scopes: [] };
    try {
      if (ext === '.sql') {
        parsed = parseSqlFile(row.file_id, filePath, row.captured_at);
        stats.sql++;
      } else if (ext === '.json') {
        parsed = parseJsonFile(row.file_id, filePath, row.captured_at);
        stats.json++;
      } else if (ext === '.log') {
        parsed = parseLogFile(row.file_id, filePath, row.captured_at);
        stats.log++;
      } else if (ext === '.txt') {
        parsed = parseTextFile(row.file_id, filePath, row.captured_at);
        stats.text++;
      } else if (ext === '.pdf' || ext === '.png' || ext === '.jpg' || ext === '.jpeg') {
        const kind = ext === '.pdf' ? 'pdf' : 'image';
        const sha = sha256File(filePath);
        const key = row.file_path;
        let text = cache[key]?.sha256 === sha ? cache[key].text : '';
        if (!text) {
          if (kind === 'pdf') text = await extractPdfText(filePath);
          else {
            text = await ocrImage(filePath, opts.codeRoot);
            stats.ocrCalls++;
          }
          cache[key] = { file: key, sha256: sha, method: kind === 'pdf' ? 'pdf-parse' : 'tesseract.js(eng, 3600px grayscale)', text };
          cacheDirty = true;
        }
        mediaText.set(row.file_id, text);
        let llm: ParsedEvidence | null = null;
        if (opts.mediaExtractor) {
          llm = await opts.mediaExtractor({ fileId: row.file_id, filePath, origin: row.origin, capturedAt: row.captured_at, text });
          if (llm) stats.llmMediaCalls++;
        }
        parsed = llm ?? interpretMediaText(row.file_id, row.origin, row.captured_at, text, catalog, kind);
        stats[kind]++;
      }
    } catch (err) {
      log(`failed to parse ${row.file_path}: ${(err as Error).message}`);
    }
    observations.push(...parsed.observations);
    for (const sc of parsed.scopes) scopes.push({ ...sc, fileCapturedAt: row.captured_at });
  }
  await shutdownOcr();
  if (cacheDirty) {
    fs.mkdirSync(path.dirname(cachePath), { recursive: true });
    fs.writeFileSync(cachePath, JSON.stringify(cache, null, 2) + '\n');
  }
  observations.push(...activityObservations(opts.activity, catalog));

  const byResource = new Map<string, Observation[]>();
  for (const o of observations) {
    const list = byResource.get(o.resourceId) ?? [];
    list.push(o);
    byResource.set(o.resourceId, list);
  }
  return { observations, scopes, catalog, byResource, manifest: new Map(opts.manifest.map((m) => [m.file_id, m])), mediaText, stats };
}

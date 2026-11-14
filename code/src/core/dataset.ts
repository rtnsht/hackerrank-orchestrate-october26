// Loads the participant-facing dataset (only files under dataset/).
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseCsv } from './csv.js';
import type { ActivityRow, AuditRow, DeletionRequest, IdentityRow, ManifestRow, RuleRow } from './types.js';

const here = path.dirname(fileURLToPath(import.meta.url));
export const CODE_ROOT = path.resolve(here, '..', '..');
export const REPO_ROOT = path.resolve(CODE_ROOT, '..');
export const DATASET_DIR = path.resolve(process.env.DATASET_DIR?.trim() || path.join(REPO_ROOT, 'dataset'));

export interface Dataset {
  dir: string;
  requests: DeletionRequest[];
  sampleRequests: DeletionRequest[];
  identities: IdentityRow[];
  audit: AuditRow[];
  activity: ActivityRow[];
  rules: RuleRow[];
  manifest: ManifestRow[];
}

function readCsv<T>(dir: string, name: string): T[] {
  const p = path.join(dir, name);
  if (!fs.existsSync(p)) return [];
  return parseCsv(fs.readFileSync(p, 'utf8')) as unknown as T[];
}

export function loadDataset(dir: string = DATASET_DIR): Dataset {
  return {
    dir,
    requests: readCsv<DeletionRequest>(dir, 'deletion_requests.csv'),
    sampleRequests: readCsv<DeletionRequest>(dir, 'sample_deletion_requests.csv'),
    identities: readCsv<IdentityRow>(dir, 'user_identities.csv'),
    audit: readCsv<AuditRow>(dir, 'user_audit_log.csv'),
    activity: readCsv<ActivityRow>(dir, 'system_activity.csv'),
    rules: readCsv<RuleRow>(dir, 'deletion_rules.csv'),
    manifest: readCsv<ManifestRow>(dir, 'evidence_files.csv'),
  };
}

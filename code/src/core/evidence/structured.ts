// Parsers for SQL snapshot exports, JSON records and plain-text handover notes.
import fs from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import type { Observation, SnapshotScope } from '../types.js';
import { interpretRecord, normSystem, systemFromResource, type RecordBase } from './records.js';

export interface ParsedEvidence {
  observations: Observation[];
  scopes: SnapshotScope[];
  text?: string; // raw text for media / free text (used by the LLM extraction path and by debugging)
}

const INVENTORY_TABLES: Record<string, string> = {
  customer_profiles: 'account_service',
  customer_profiles_primary: 'account_service',
  customer_profiles_failover_replica: 'account_service',
  customer_search_documents: 'search_service',
  search_documents_primary: 'search_service',
  customer_analytics_profiles: 'analytics_platform',
  support_records: 'support_platform',
  customer_exports: 'export_service',
  connector_subscriptions: 'integration_service',
  backup_restore_entries: 'backup_service',
  security_audit_records: 'security_service',
};

function addScope(scopes: Map<string, SnapshotScope>, evidenceId: string, system: string, capturedAt: string, copy: string, resourceId?: string) {
  const key = `${system}|${capturedAt}`;
  let s = scopes.get(key);
  if (!s) {
    s = { evidenceId, system, capturedAt, replicaCopies: 0, copies: new Set(), resources: new Set() };
    scopes.set(key, s);
  }
  const copies = s.copies!;
  copies.add(copy);
  s.replicaCopies = copies.size;
  if (resourceId) s.resources.add(resourceId);
}

export function parseSqlFile(fileId: string, filePath: string, capturedAt: string): ParsedEvidence {
  const sql = fs.readFileSync(filePath, 'utf8');
  const db = new DatabaseSync(':memory:');
  db.exec(sql);
  const header = sql.match(/Captured at:\s*(\S+)/);
  const snapAt = header ? header[1] : capturedAt;
  const tables = (db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all() as { name: string }[]).map((t) => t.name);
  const observations: Observation[] = [];
  const scopes = new Map<string, SnapshotScope>();
  const scopeSystems = new Map<string, string>();
  if (tables.includes('snapshot_scope')) {
    for (const r of db.prepare('SELECT origin_system, captured_at FROM snapshot_scope').all() as { origin_system: string; captured_at: string }[]) {
      scopeSystems.set(normSystem(r.origin_system), r.captured_at);
    }
  }
  for (const table of tables) {
    if (table === 'snapshot_scope') continue;
    const rows = db.prepare(`SELECT * FROM "${table}"`).all() as Record<string, unknown>[];
    const base: RecordBase = { evidenceId: fileId, kind: 'file', capturedAt, observedAt: snapAt, parser: 'sql', copy: table };
    if (INVENTORY_TABLES[table]) {
      const system = INVENTORY_TABLES[table];
      const at = scopeSystems.get(system) ?? snapAt;
      if (!tables.includes('snapshot_scope') || scopeSystems.has(system)) addScope(scopes, fileId, system, at, table);
      for (const row of rows) {
        const rec: Record<string, unknown> = { ...row };
        if ('user_id' in rec && !('customer_reference' in rec)) rec.customer_reference = rec.user_id;
        const obs = interpretRecord({ ...base, observedAt: at }, system, table, rec);
        if (obs) {
          obs.copy = table;
          observations.push(obs);
          if (!tables.includes('snapshot_scope') || scopeSystems.has(system)) addScope(scopes, fileId, system, at, table, obs.resourceId);
        }
      }
      continue;
    }
    // Generic replica tables: resource_id, origin_system, collection_name, record_json
    const cols = rows.length ? Object.keys(rows[0]) : [];
    if (cols.includes('record_json') || /^customer_data_/.test(table)) {
      for (const row of rows) {
        let rec: Record<string, unknown> = {};
        try {
          rec = JSON.parse(String(row.record_json));
        } catch {
          continue;
        }
        const system = normSystem(String(row.origin_system ?? systemFromResource(String(row.resource_id)) ?? 'unknown'));
        const obs = interpretRecord(base, system, String(row.collection_name ?? ''), { resource_id: row.resource_id, ...rec });
        if (obs) {
          obs.copy = table;
          observations.push(obs);
          addScope(scopes, fileId, system, snapAt, table, obs.resourceId);
        }
      }
      // tables with no rows still count as copies for systems already seen
    }
  }
  // register empty copies for multi-copy topologies so replicaCopies reflects all compared tables
  const genericTables = tables.filter((t) => /^customer_data_/.test(t));
  for (const s of scopes.values()) {
    for (const t of genericTables) {
      const copies = s.copies!;
      if (!copies.has(t) && s.evidenceId === fileId && genericTables.length > 1) {
        copies.add(t);
        s.replicaCopies = copies.size;
      }
    }
  }
  db.close();
  return { observations, scopes: [...scopes.values()] };
}

export function parseJsonFile(fileId: string, filePath: string, capturedAt: string): ParsedEvidence {
  const data = JSON.parse(fs.readFileSync(filePath, 'utf8')) as Record<string, unknown>;
  const observations: Observation[] = [];
  const scopes = new Map<string, SnapshotScope>();
  const base = (observedAt: string, parser: string): RecordBase => ({ evidenceId: fileId, kind: 'file', capturedAt, observedAt, parser });

  if (Array.isArray(data.snapshots)) {
    // records-assurance snapshot bundle
    for (const snap of data.snapshots as Record<string, unknown>[]) {
      const at = String(snap.captured_at ?? capturedAt);
      const store = normSystem(String(snap.data_store ?? ''));
      const collection = String(snap.collection ?? '');
      const records = (snap.records as Record<string, unknown>[]) ?? [];
      const lookupStyle = collection === 'operational_record_history';
      if (!lookupStyle) addScope(scopes, fileId, store, at, collection);
      for (const rec of records) {
        const obs = interpretRecord({ ...base(at, 'json-bundle'), copy: collection }, lookupStyle ? '' : store, collection, rec);
        if (obs) {
          obs.copy = collection;
          observations.push(obs);
          if (!lookupStyle) addScope(scopes, fileId, store, at, collection, obs.resourceId);
        }
      }
    }
  } else if (data.service_exports && typeof data.service_exports === 'object') {
    for (const [system, sections] of Object.entries(data.service_exports as Record<string, Record<string, Record<string, unknown>[]>>)) {
      for (const [section, records] of Object.entries(sections)) {
        for (const rec of records) {
          const obs = interpretRecord(base(String(rec.observed_at ?? capturedAt), 'json-export'), system, section, rec);
          if (obs) observations.push(obs);
        }
      }
    }
  } else if (Array.isArray(data.records)) {
    const collection = String(data.collection ?? data.origin ?? '');
    for (const rec of data.records as Record<string, unknown>[]) {
      const sys = String(rec.system ?? systemFromResource(String(rec.resource_id ?? '')) ?? '');
      const obs = interpretRecord(base(String(rec.recorded_at ?? data.captured_at ?? capturedAt), 'json-records'), sys, collection, rec);
      if (obs) observations.push(obs);
    }
  }
  return { observations, scopes: [...scopes.values()] };
}

/** Plain-text handover notes: "- <time> | <system> | <resource> | <TYPE> | <RESULT> | <subject>" */
export function parseTextFile(fileId: string, filePath: string, capturedAt: string): ParsedEvidence {
  const text = fs.readFileSync(filePath, 'utf8');
  const observations: Observation[] = [];
  for (const line of text.split(/\r?\n/)) {
    const parts = line.replace(/^\s*-\s*/, '').split('|').map((p) => p.trim());
    if (parts.length < 6 || !/_\d+$/.test(parts[2])) continue;
    const [at, system, resource_id, record_type, result, subject] = parts;
    const obs = interpretRecord(
      { evidenceId: fileId, kind: 'file', capturedAt, observedAt: at || capturedAt, parser: 'text-handover' },
      system,
      'handover',
      { system, resource_id, record_type, result, subject_reference: subject, recorded_at: at },
    );
    if (obs) observations.push(obs);
  }
  return { observations, scopes: [], text };
}

// Participant-created evaluation cases. Writes a small, fully synthetic dataset (same layout as dataset/) whose
// expected answers were derived by hand from the challenge rules, so the audit agent can be scored on behaviours the
// public samples do not isolate. All people, emails and ids are fictional (example.com).
import fs from 'node:fs';
import path from 'node:path';

export interface CustomCase {
  case_id: string;
  title: string;
  checks: string;
  expected_status: 'COMPLETE' | 'INCOMPLETE' | 'NOT_APPLICABLE';
  expected_steps: { phase: string; system_id: string; resource_id: string; operation: string; state: string; evidence_id: string }[];
}

const REQ = { requested_at: '2026-10-01T12:00:00Z', reported_at: '2026-10-02T12:00:00Z', audit_at: '2026-10-05T12:00:00Z' };

const v = (system_id: string, resource_id: string, operation: string, state: string, evidence_id = '') => ({ phase: 'VERIFICATION', system_id, resource_id, operation, state, evidence_id });
const f = (system_id: string, resource_id: string, operation: string, state: string, evidence_id: string) => ({ phase: 'FINDING', system_id, resource_id, operation, state, evidence_id });
const r = (system_id: string, resource_id: string, operation: string, state: string) => ({ phase: 'REMEDIATION', system_id, resource_id, operation, state, evidence_id: '' });

export const CUSTOM_CASES: (CustomCase & { email: string; reported_status: string; request_text: string })[] = [
  {
    case_id: 'case_c01',
    title: 'Historical email + identifiable analytics',
    checks: 'Request arrives under a historical email; the analytics profile still carries that old email and must be anonymized, the security audit record retained.',
    email: 'ana.old@example.com',
    reported_status: 'COMPLETE',
    request_text: 'Please delete my account. I used this older address before changing it.',
    expected_status: 'INCOMPLETE',
    expected_steps: [
      f('analytics_platform', 'analytics_9001', 'REPORT_EXPOSED_PII', 'PRESENT', 'evidence_c01'),
      r('analytics_platform', 'analytics_9001', 'ANONYMIZE_RECORD', 'ANONYMIZED'),
      v('account_service', 'account_9001', 'VERIFY_RECORD_ABSENT', 'ABSENT'),
      v('security_service', 'audit_record_9001', 'VERIFY_RECORD_PRESENT', 'PRESENT'),
      v('analytics_platform', 'analytics_9001', 'VERIFY_RECORD_ANONYMIZED', 'ANONYMIZED'),
    ],
  },
  {
    case_id: 'case_c02',
    title: 'Replication drift',
    checks: 'The primary profile table no longer has the account but the failover replica still does: the deletion is incomplete.',
    email: 'ben.drift@example.com',
    reported_status: 'COMPLETE',
    request_text: 'Please permanently delete the account associated with this email address.',
    expected_status: 'INCOMPLETE',
    expected_steps: [
      f('account_service', 'account_9002', 'REPORT_REMAINING_RECORD', 'PRESENT', 'evidence_c02'),
      r('account_service', 'account_9002', 'DELETE_RECORD', 'ABSENT'),
      v('account_service', 'account_9002', 'VERIFY_RECORD_ABSENT', 'ABSENT'),
    ],
  },
  {
    case_id: 'case_c03',
    title: 'Evidence captured after audit_at is ignored',
    checks: 'A search hit appears only in an export captured after audit_at; at audit_at the index query returned no hit, so the case is COMPLETE and cites only pre-audit evidence.',
    email: 'cara.later@example.com',
    reported_status: 'COMPLETE',
    request_text: 'I would like the account associated with this address to be erased.',
    expected_status: 'COMPLETE',
    expected_steps: [
      v('account_service', 'account_9003', 'VERIFY_RECORD_ABSENT', 'ABSENT', 'evidence_c05'),
      v('search_service', 'search_9003', 'VERIFY_RECORD_ABSENT', 'ABSENT', 'evidence_c01'),
    ],
  },
  {
    case_id: 'case_c04',
    title: 'Account tombstone still exposing PII',
    checks: 'The deleted-account reference may keep the stable user_id but its email must be redacted.',
    email: 'dev.tomb@example.com',
    reported_status: 'PARTIALLY_COMPLETE',
    request_text: 'Please close my account. Keeping an internal reference is fine, but remove my contact details.',
    expected_status: 'INCOMPLETE',
    expected_steps: [
      f('identity_service', 'tombstone_9004', 'REPORT_EXPOSED_PII', 'PRESENT', 'evidence_c03'),
      r('identity_service', 'tombstone_9004', 'REDACT_RECORD', 'REDACTED'),
      v('account_service', 'account_9004', 'VERIFY_RECORD_ABSENT', 'ABSENT'),
      v('identity_service', 'tombstone_9004', 'VERIFY_RECORD_REDACTED', 'REDACTED'),
    ],
  },
  {
    case_id: 'case_c05',
    title: 'Active sync + open restore path + remaining search document (remediation order)',
    checks: 'Findings in operation order; remediation disables the flow and blocks the restore before deleting the search document.',
    email: 'eva.flow@example.com',
    reported_status: 'COMPLETE',
    request_text: 'Close the account completely and make sure no integration or backup can bring my data back.',
    expected_status: 'INCOMPLETE',
    expected_steps: [
      f('search_service', 'search_9005', 'REPORT_REMAINING_RECORD', 'PRESENT', 'evidence_c01'),
      f('integration_service', 'data_flow_9005', 'REPORT_ACTIVE_DATA_FLOW', 'ACTIVE', 'evidence_c01'),
      f('backup_service', 'backup_9005', 'REPORT_RESTORE_RISK', 'ACTIVE', 'evidence_c01'),
      r('integration_service', 'data_flow_9005', 'DISABLE_DATA_FLOW', 'DISABLED'),
      r('backup_service', 'backup_9005', 'BLOCK_RESTORE', 'BLOCKED'),
      r('search_service', 'search_9005', 'DELETE_RECORD', 'ABSENT'),
      v('account_service', 'account_9005', 'VERIFY_RECORD_ABSENT', 'ABSENT'),
      v('search_service', 'search_9005', 'VERIFY_RECORD_ABSENT', 'ABSENT'),
      v('integration_service', 'data_flow_9005', 'VERIFY_DATA_FLOW_DISABLED', 'DISABLED'),
      v('backup_service', 'backup_9005', 'VERIFY_RESTORE_BLOCKED', 'BLOCKED'),
    ],
  },
  {
    case_id: 'case_c06',
    title: 'Spanish non-deletion request with deletion vocabulary',
    checks: 'The message says "do not delete" and asks for a copy of the data: NOT_APPLICABLE even though it contains a deletion verb.',
    email: 'finn.keep@example.com',
    reported_status: 'NOT_STARTED',
    request_text: 'Por favor, no borren mi cuenta. Solo necesito una copia de mis datos personales.',
    expected_status: 'NOT_APPLICABLE',
    expected_steps: [],
  },
  {
    case_id: 'case_c07',
    title: 'Look-alike decoy email',
    checks: 'A search document for gus.decoy.jr@example.com belongs to someone else and must not be attributed to gus.decoy@example.com.',
    email: 'gus.decoy@example.com',
    reported_status: 'COMPLETE',
    request_text: 'Please process a permanent account-deletion request for this email address.',
    expected_status: 'COMPLETE',
    expected_steps: [v('account_service', 'account_9007', 'VERIFY_RECORD_ABSENT', 'ABSENT', 'evidence_c05')],
  },
  {
    case_id: 'case_c08',
    title: 'Korean marketing opt-out that explicitly refuses deletion',
    checks: 'Contains 삭제 (delete) but says not to delete the account: NOT_APPLICABLE.',
    email: 'hana.optout@example.com',
    reported_status: 'NOT_STARTED',
    request_text: '계정은 삭제하지 마세요. 마케팅 이메일 수신만 중단해 주세요.',
    expected_status: 'NOT_APPLICABLE',
    expected_steps: [],
  },
];

const csvLine = (vals: string[]) => vals.map((x) => (/[",\n]/.test(x) ? `"${x.replace(/"/g, '""')}"` : x)).join(',');

export function writeCustomDataset(dir: string, rulesCsvPath: string): void {
  fs.rmSync(dir, { recursive: true, force: true });
  for (const d of ['evidence/records', 'evidence/databases', 'evidence/logs']) fs.mkdirSync(path.join(dir, d), { recursive: true });
  fs.copyFileSync(rulesCsvPath, path.join(dir, 'deletion_rules.csv'));
  fs.writeFileSync(
    path.join(dir, 'deletion_requests.csv'),
    ['case_id,email,requested_at,reported_status,reported_at,audit_at,request_text', ...CUSTOM_CASES.map((c) => csvLine([c.case_id, c.email, REQ.requested_at, c.reported_status, REQ.reported_at, REQ.audit_at, c.request_text]))].join('\n') + '\n',
  );
  fs.writeFileSync(path.join(dir, 'sample_deletion_requests.csv'), 'case_id,email,requested_at,reported_status,reported_at,audit_at,request_text\n');
  fs.writeFileSync(
    path.join(dir, 'user_identities.csv'),
    [
      'user_id,name,email,phone,email_hash,updated_at',
      'user_901,Ana Testa,ana.current@example.com,+1 555 010 0001,1111111111111111111111111111111111111111111111111111111111111111,2026-06-01T00:00:00Z',
      'user_902,Ben Drift,ben.drift@example.com,+1 555 010 0002,2222222222222222222222222222222222222222222222222222222222222222,2026-01-01T00:00:00Z',
      'user_903,Cara Later,cara.later@example.com,+1 555 010 0003,3333333333333333333333333333333333333333333333333333333333333333,2026-01-01T00:00:00Z',
      'user_904,Dev Tomb,dev.tomb@example.com,+1 555 010 0004,4444444444444444444444444444444444444444444444444444444444444444,2026-01-01T00:00:00Z',
      'user_905,Eva Flow,eva.flow@example.com,+1 555 010 0005,5555555555555555555555555555555555555555555555555555555555555555,2026-01-01T00:00:00Z',
      'user_906,Finn Keep,finn.keep@example.com,+1 555 010 0006,6666666666666666666666666666666666666666666666666666666666666666,2026-01-01T00:00:00Z',
      'user_907,Gus Decoy,gus.decoy@example.com,+1 555 010 0007,7777777777777777777777777777777777777777777777777777777777777777,2026-01-01T00:00:00Z',
      'user_908,Hana Optout,hana.optout@example.com,+1 555 010 0008,8888888888888888888888888888888888888888888888888888888888888888,2026-01-01T00:00:00Z',
      'user_909,Gus Decoy Junior,gus.decoy.jr@example.com,+1 555 010 0009,9999999999999999999999999999999999999999999999999999999999999999,2026-01-01T00:00:00Z',
    ].join('\n') + '\n',
  );
  fs.writeFileSync(
    path.join(dir, 'user_audit_log.csv'),
    ['audit_id,user_id,event_time,field_name,old_value,new_value', 'audit_c01,user_901,2026-06-01T00:00:00Z,email,ana.old@example.com,ana.current@example.com'].join('\n') + '\n',
  );
  fs.writeFileSync(
    path.join(dir, 'system_activity.csv'),
    [
      'activity_id,event_time,event_type,source_system,target_system,resource_id,reported_result,details',
      // a deletion-worker claim that is contradicted by the replica evidence (must not be trusted)
      'activity_c01,2026-10-02T11:00:00Z,DELETE_RECORD,deletion_worker,account_service,account_9002,SUCCESS,Deletion worker reported the requested operation as completed. Target resource: account_9002.',
    ].join('\n') + '\n',
  );
  const manifest = [
    ['evidence_c01', 'evidence/records/service_resource_export_20261004.json', 'service_resource_export', '2026-10-04T10:00:00Z'],
    ['evidence_c02', 'evidence/databases/customer_accounts_failover_replica_20261004.sql', 'customer_account_failover_replica_capture', '2026-10-04T11:00:00Z'],
    ['evidence_c03', 'evidence/records/customer_data_snapshot_bundle_20261004.json', 'records_assurance_snapshot_bundle', '2026-10-04T12:00:00Z'],
    ['evidence_c04', 'evidence/records/service_resource_export_20261006.json', 'service_resource_export', '2026-10-06T10:00:00Z'],
    ['evidence_c05', 'evidence/logs/resource_lookup_trace_20261004.log', 'service_readback_logs', '2026-10-04T09:00:00Z'],
  ];
  fs.writeFileSync(path.join(dir, 'evidence_files.csv'), ['file_id,file_path,origin,captured_at', ...manifest.map((m) => m.join(','))].join('\n') + '\n');

  const exp = (at: string, extraSearch: boolean) => ({
    exported_at: at,
    region: 'global',
    service_exports: {
      analytics_platform: {
        profiles: [{ audience_cohort: 'general_shopper', customer_reference: null, email: 'ana.old@example.com', email_hash: null, observed_at: '2026-10-04T09:30:00Z', phone: null, resource_id: 'analytics_9001' }],
      },
      security_service: {
        audit_records: [{ event_code: 'ACCOUNT_DELETION_RECORDED', observed_at: '2026-10-04T09:31:00Z', resource_id: 'audit_record_9001', retention_until: '2033-12-31T23:59:59Z', subject_reference: 'user_901' }],
      },
      search_service: {
        index_queries: [
          { hits: { documents: extraSearch ? [{ resource_id: 'search_9003', email: 'cara.later@example.com' }] : [], total: extraSearch ? 1 : 0 }, observed_at: extraSearch ? '2026-10-06T09:00:00Z' : '2026-10-04T09:32:00Z', query: 'cara.later@example.com', resource_id: 'search_9003' },
          ...(extraSearch
            ? []
            : [
                { hits: { documents: [{ resource_id: 'search_9005', email: 'eva.flow@example.com', display_name: 'Eva Flow' }], total: 1 }, observed_at: '2026-10-04T09:33:00Z', query: 'eva.flow@example.com', resource_id: 'search_9005' },
                { hits: { documents: [{ resource_id: 'search_9907', email: 'gus.decoy.jr@example.com' }], total: 1 }, observed_at: '2026-10-04T09:34:00Z', query: 'gus.decoy.jr@example.com', resource_id: 'search_9907' },
              ]),
        ],
      },
      ...(extraSearch
        ? {}
        : {
            integration_service: {
              connectors: [{ connector_name: 'customer_events_connector', customer_email: 'eva.flow@example.com', customer_reference: null, enabled: true, last_delivery_status: 'delivered', observed_at: '2026-10-04T09:35:00Z', resource_id: 'data_flow_9005' }],
            },
            backup_service: {
              restore_entries: [{ archive_key: 'archive/customer-data/backup_9005.tar.zst', customer_email: null, customer_reference: 'user_905', observed_at: '2026-10-04T09:36:00Z', resource_id: 'backup_9005', restore_allowed: true }],
            },
          }),
    },
  });
  fs.writeFileSync(path.join(dir, 'evidence/records/service_resource_export_20261004.json'), JSON.stringify(exp('2026-10-04T10:00:00Z', false), null, 2));
  fs.writeFileSync(path.join(dir, 'evidence/records/service_resource_export_20261006.json'), JSON.stringify(exp('2026-10-06T10:00:00Z', true), null, 2));
  fs.writeFileSync(
    path.join(dir, 'evidence/databases/customer_accounts_failover_replica_20261004.sql'),
    `-- Captured at: 2026-10-04T11:00:00Z
BEGIN TRANSACTION;
CREATE TABLE customer_profiles_primary (resource_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, full_name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL, account_status TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE customer_profiles_failover_replica (resource_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, full_name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL, account_status TEXT NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO "customer_profiles_failover_replica" VALUES('account_9002','user_902','Ben Drift','ben.drift@example.com','+1 555 010 0002','ACTIVE','2026-10-04T10:45:00Z');
COMMIT;
`,
  );
  fs.writeFileSync(
    path.join(dir, 'evidence/records/customer_data_snapshot_bundle_20261004.json'),
    JSON.stringify(
      {
        exported_at: '2026-10-04T12:00:00Z',
        source: 'records_assurance_service',
        snapshots: [
          {
            captured_at: '2026-10-04T12:00:00Z',
            data_store: 'identity_service',
            collection: 'deleted_customer_references',
            records: [{ resource_id: 'tombstone_9004', subject_reference: 'user_904', name: null, email: 'dev.tomb@example.com', phone: null, email_hash: null, account_status: 'DELETED' }],
          },
        ],
      },
      null,
      2,
    ),
  );
  const lookups = [
    ['account_9001', 'ana.current@example.com'],
    ['account_9003', 'cara.later@example.com'],
    ['account_9004', 'dev.tomb@example.com'],
    ['account_9005', 'eva.flow@example.com'],
    ['account_9007', 'gus.decoy@example.com'],
  ];
  const log = lookups
    .flatMap(([rid, key], i) => {
      const t = `2026-10-04T08:0${i}:00Z`;
      const tr = `rb-080${i}00-00${i + 1}`;
      return [
        `${t} level=DEBUG service=account_service trace_id=${tr} event=profile_lookup_started resource_id=${rid} lookup_key=${key}`,
        `${t} level=INFO service=account_service trace_id=${tr} event=profile_lookup_completed resource_id=${rid} rows=0 http_status=404`,
      ];
    })
    .join('\n');
  fs.writeFileSync(path.join(dir, 'evidence/logs/resource_lookup_trace_20261004.log'), log + '\n');
}

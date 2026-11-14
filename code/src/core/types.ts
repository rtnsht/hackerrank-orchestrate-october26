// Shared domain types for the deletion-audit engine.

export type DeletionStatus = 'COMPLETE' | 'INCOMPLETE' | 'NOT_APPLICABLE';
export type Phase = 'FINDING' | 'REMEDIATION' | 'VERIFICATION';

export interface DeletionRequest {
  case_id: string;
  email: string;
  requested_at: string;
  reported_status: string;
  reported_at: string;
  audit_at: string;
  request_text: string;
}

export interface IdentityRow {
  user_id: string;
  name: string;
  email: string;
  phone: string;
  email_hash: string;
  updated_at: string;
}

export interface AuditRow {
  audit_id: string;
  user_id: string;
  event_time: string;
  field_name: string;
  old_value: string;
  new_value: string;
}

export interface ActivityRow {
  activity_id: string;
  event_time: string;
  event_type: string;
  source_system: string;
  target_system: string;
  resource_id: string;
  reported_result: string;
  details: string;
}

export interface RuleRow {
  rule_id: string;
  data_category: string;
  condition: string;
  required_action: string;
}

export interface ManifestRow {
  file_id: string;
  file_path: string;
  origin: string;
  captured_at: string;
}

/** Normalised state of a resource as observed in one piece of evidence. */
export type ObservedState =
  | 'PRESENT' // record exists (account/search/export/attachment/... or retained record)
  | 'ABSENT' // record proven not to exist
  | 'IDENTIFIABLE' // record exists and still exposes direct identifiers
  | 'ANONYMIZED' // record exists, no longer attributable
  | 'REDACTED' // record exists, prohibited identifiers removed
  | 'ACTIVE' // data flow enabled
  | 'DISABLED' // data flow disabled
  | 'RESTORABLE' // backup restore path open
  | 'BLOCKED'; // backup restore blocked

/**
 * One fact extracted from one evidence item. Identifier tokens are normalised:
 *   email:<lowercase>  user:<user_id>  phone:<digits>  hash:<sha256>  name:<folded name>  res:<resource_id>
 */
export interface Observation {
  evidenceId: string; // file_id or activity_id
  kind: 'file' | 'activity';
  capturedAt: string; // when the evidence item was captured (cut-off check)
  observedAt: string; // record-level observation time
  system: string;
  resourceId: string;
  category?: string; // deletion_rules data_category when determinable
  state?: ObservedState; // undefined => only links identity
  identifiers: string[];
  /** tokens that identify the record's subject but are permitted to remain (e.g. tombstone subject ref) */
  claim?: boolean; // workflow/activity claims (not proof)
  copy?: string; // table / replica name that held the record
  parser: string;
  note?: string;
}

/** A system/time scope that an evidence item fully enumerates (absence => ABSENT). */
export interface SnapshotScope {
  evidenceId: string;
  system: string;
  capturedAt: string;
  fileCapturedAt?: string; // manifest capture time of the file holding the snapshot
  replicaCopies: number; // number of copies (primary + replicas) compared in the snapshot
  copies?: Set<string>; // table / collection names enumerated for this system
  resources: Set<string>;
}

export interface Step {
  step_no: number;
  phase: Phase;
  system_id: string;
  resource_id: string;
  operation: string;
  state: string;
  evidence_id: string;
}

export interface CaseResult {
  case_id: string;
  deletion_status: DeletionStatus;
  steps: Step[];
  trace?: unknown;
}

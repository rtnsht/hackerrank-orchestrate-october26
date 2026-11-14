// Deletion policy: maps data categories to required treatments using dataset/deletion_rules.csv.
import { iso } from './normalize.js';
import type { ObservedState, RuleRow } from './types.js';

export type Treatment = 'DELETE' | 'REDACT' | 'ANONYMIZE' | 'RETAIN' | 'BLOCK_RESTORE' | 'DISABLE_FLOW';

export interface PolicyDecision {
  treatment: Treatment;
  ruleId: string;
  rationale: string;
}

/** Internal categories that are refinements of a rules-file category. */
const CATEGORY_ALIASES: Record<string, { category: string; conditionHolds: boolean; note: string }> = {
  // A support contact copy with no shared conversation context does not satisfy rule_05's condition,
  // so the record is personal data of the subject only and is removed entirely.
  SUPPORT_CONTACT_RECORD: { category: 'SUPPORT_MESSAGE', conditionHolds: false, note: 'support contact copy without shared conversation context' },
};

export class DeletionPolicy {
  private byCategory = new Map<string, RuleRow>();
  constructor(rules: RuleRow[]) {
    for (const r of rules) this.byCategory.set(r.data_category, r);
  }

  decide(category: string | undefined, system: string, auditAt: string, note?: string): PolicyDecision | null {
    if (system === 'integration_service' || category === 'DATA_FLOW') {
      return { treatment: 'DISABLE_FLOW', ruleId: 'principle:no-active-recreation', rationale: 'Active sync/import paths must be disabled so deleted data cannot be recreated.' };
    }
    if (!category) return null;
    const alias = CATEGORY_ALIASES[category];
    if (alias) {
      const rule = this.byCategory.get(alias.category);
      if (!alias.conditionHolds) {
        return { treatment: 'DELETE', ruleId: rule?.rule_id ?? 'rule_05', rationale: `${alias.note}; ${rule?.rule_id ?? 'rule_05'} condition not met -> delete` };
      }
    }
    const rule = this.byCategory.get(category);
    if (!rule) return null;
    const action = rule.required_action.toUpperCase();
    switch (action) {
      case 'DELETE':
        return { treatment: 'DELETE', ruleId: rule.rule_id, rationale: `${rule.rule_id}: ${category} -> DELETE (${rule.condition})` };
      case 'REDACT':
        return { treatment: 'REDACT', ruleId: rule.rule_id, rationale: `${rule.rule_id}: ${category} -> REDACT (${rule.condition})` };
      case 'ANONYMIZE':
        return { treatment: 'ANONYMIZE', ruleId: rule.rule_id, rationale: `${rule.rule_id}: ${category} -> ANONYMIZE (${rule.condition})` };
      case 'RETAIN': {
        if (category === 'BACKUP_ARCHIVE') {
          return { treatment: 'BLOCK_RESTORE', ruleId: rule.rule_id, rationale: `${rule.rule_id}: backups are retained only if restore access is blocked` };
        }
        // retention conditions: within supplied retention period / legal hold active
        const until = note?.match(/(?:retention_until|retained_until)=(\S+)/)?.[1];
        if (until && iso(until) > 0 && iso(until) < iso(auditAt)) {
          return { treatment: 'DELETE', ruleId: rule.rule_id, rationale: `${rule.rule_id}: retention ended ${until}; record no longer protected` };
        }
        return { treatment: 'RETAIN', ruleId: rule.rule_id, rationale: `${rule.rule_id}: ${category} -> RETAIN (${rule.condition})` };
      }
      default:
        return null;
    }
  }
}

export interface StepPlan {
  finding?: { operation: string; state: string };
  remediation?: { operation: string; state: string };
  verification: { operation: string; state: string };
  compliant: boolean;
}

/** Given the required treatment and the reconstructed state, decide finding/remediation/verification. */
export function planFor(treatment: Treatment, state: ObservedState | undefined): StepPlan | null {
  switch (treatment) {
    case 'DELETE':
      if (state === 'ABSENT') return { verification: { operation: 'VERIFY_RECORD_ABSENT', state: 'ABSENT' }, compliant: true };
      if (!state) return null;
      return {
        finding: { operation: 'REPORT_REMAINING_RECORD', state: 'PRESENT' },
        remediation: { operation: 'DELETE_RECORD', state: 'ABSENT' },
        verification: { operation: 'VERIFY_RECORD_ABSENT', state: 'ABSENT' },
        compliant: false,
      };
    case 'REDACT':
      if (state === 'REDACTED') return { verification: { operation: 'VERIFY_RECORD_REDACTED', state: 'REDACTED' }, compliant: true };
      if (state === 'ABSENT') return { verification: { operation: 'VERIFY_RECORD_ABSENT', state: 'ABSENT' }, compliant: true };
      if (!state) return null;
      return {
        finding: { operation: 'REPORT_EXPOSED_PII', state: 'PRESENT' },
        remediation: { operation: 'REDACT_RECORD', state: 'REDACTED' },
        verification: { operation: 'VERIFY_RECORD_REDACTED', state: 'REDACTED' },
        compliant: false,
      };
    case 'ANONYMIZE':
      if (state === 'ANONYMIZED') return { verification: { operation: 'VERIFY_RECORD_ANONYMIZED', state: 'ANONYMIZED' }, compliant: true };
      if (state === 'ABSENT') return { verification: { operation: 'VERIFY_RECORD_ABSENT', state: 'ABSENT' }, compliant: true };
      if (!state) return null;
      return {
        finding: { operation: 'REPORT_EXPOSED_PII', state: 'PRESENT' },
        remediation: { operation: 'ANONYMIZE_RECORD', state: 'ANONYMIZED' },
        verification: { operation: 'VERIFY_RECORD_ANONYMIZED', state: 'ANONYMIZED' },
        compliant: false,
      };
    case 'RETAIN':
      // retained records are verified present; a missing retained record cannot be remediated by deletion steps
      if (!state) return null;
      return { verification: { operation: 'VERIFY_RECORD_PRESENT', state: 'PRESENT' }, compliant: true };
    case 'BLOCK_RESTORE':
      if (state === 'BLOCKED' || state === 'ABSENT') return { verification: { operation: 'VERIFY_RESTORE_BLOCKED', state: 'BLOCKED' }, compliant: true };
      if (!state) return null;
      return {
        finding: { operation: 'REPORT_RESTORE_RISK', state: 'ACTIVE' },
        remediation: { operation: 'BLOCK_RESTORE', state: 'BLOCKED' },
        verification: { operation: 'VERIFY_RESTORE_BLOCKED', state: 'BLOCKED' },
        compliant: false,
      };
    case 'DISABLE_FLOW':
      if (state === 'DISABLED' || state === 'ABSENT') return { verification: { operation: 'VERIFY_DATA_FLOW_DISABLED', state: 'DISABLED' }, compliant: true };
      if (!state) return null;
      return {
        finding: { operation: 'REPORT_ACTIVE_DATA_FLOW', state: 'ACTIVE' },
        remediation: { operation: 'DISABLE_DATA_FLOW', state: 'DISABLED' },
        verification: { operation: 'VERIFY_DATA_FLOW_DISABLED', state: 'DISABLED' },
        compliant: false,
      };
  }
}

/** The verification that proves a record reached the state its treatment requires. */
export function requiredVerification(treatment: Treatment): { operation: string; state: string } {
  switch (treatment) {
    case 'DELETE':
      return { operation: 'VERIFY_RECORD_ABSENT', state: 'ABSENT' };
    case 'REDACT':
      return { operation: 'VERIFY_RECORD_REDACTED', state: 'REDACTED' };
    case 'ANONYMIZE':
      return { operation: 'VERIFY_RECORD_ANONYMIZED', state: 'ANONYMIZED' };
    case 'RETAIN':
      return { operation: 'VERIFY_RECORD_PRESENT', state: 'PRESENT' };
    case 'BLOCK_RESTORE':
      return { operation: 'VERIFY_RESTORE_BLOCKED', state: 'BLOCKED' };
    case 'DISABLE_FLOW':
      return { operation: 'VERIFY_DATA_FLOW_DISABLED', state: 'DISABLED' };
  }
}

export const OPERATION_ORDER: Record<string, number> = {
  REPORT_REMAINING_RECORD: 0,
  REPORT_EXPOSED_PII: 1,
  REPORT_ACTIVE_DATA_FLOW: 2,
  REPORT_RESTORE_RISK: 3,
  DISABLE_DATA_FLOW: 0,
  BLOCK_RESTORE: 1,
  DELETE_RECORD: 2,
  REDACT_RECORD: 3,
  ANONYMIZE_RECORD: 4,
  VERIFY_RECORD_ABSENT: 0,
  VERIFY_RECORD_PRESENT: 1,
  VERIFY_RECORD_REDACTED: 2,
  VERIFY_RECORD_ANONYMIZED: 3,
  VERIFY_DATA_FLOW_DISABLED: 4,
  VERIFY_RESTORE_BLOCKED: 5,
};
export const PHASE_ORDER = { FINDING: 0, REMEDIATION: 1, VERIFICATION: 2 } as const;

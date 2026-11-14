// Output-contract validation (run for every case before output.csv is written).
import { iso } from './normalize.js';
import { OPERATION_ORDER, PHASE_ORDER } from './policy.js';
import type { CaseResult, DeletionRequest, Phase } from './types.js';
import type { AuditContext } from './pipeline.js';

const ALLOWED: Record<string, { phase: Phase; state: string }> = {
  REPORT_REMAINING_RECORD: { phase: 'FINDING', state: 'PRESENT' },
  REPORT_EXPOSED_PII: { phase: 'FINDING', state: 'PRESENT' },
  REPORT_ACTIVE_DATA_FLOW: { phase: 'FINDING', state: 'ACTIVE' },
  REPORT_RESTORE_RISK: { phase: 'FINDING', state: 'ACTIVE' },
  DISABLE_DATA_FLOW: { phase: 'REMEDIATION', state: 'DISABLED' },
  BLOCK_RESTORE: { phase: 'REMEDIATION', state: 'BLOCKED' },
  DELETE_RECORD: { phase: 'REMEDIATION', state: 'ABSENT' },
  REDACT_RECORD: { phase: 'REMEDIATION', state: 'REDACTED' },
  ANONYMIZE_RECORD: { phase: 'REMEDIATION', state: 'ANONYMIZED' },
  VERIFY_RECORD_ABSENT: { phase: 'VERIFICATION', state: 'ABSENT' },
  VERIFY_RECORD_PRESENT: { phase: 'VERIFICATION', state: 'PRESENT' },
  VERIFY_RECORD_REDACTED: { phase: 'VERIFICATION', state: 'REDACTED' },
  VERIFY_RECORD_ANONYMIZED: { phase: 'VERIFICATION', state: 'ANONYMIZED' },
  VERIFY_DATA_FLOW_DISABLED: { phase: 'VERIFICATION', state: 'DISABLED' },
  VERIFY_RESTORE_BLOCKED: { phase: 'VERIFICATION', state: 'BLOCKED' },
};

let evidenceTimes: Map<string, string> | null = null;
function evidenceTime(ctx: AuditContext, id: string): string | undefined {
  if (!evidenceTimes) {
    evidenceTimes = new Map();
    for (const m of ctx.dataset.manifest) evidenceTimes.set(m.file_id, m.captured_at);
    for (const a of ctx.dataset.activity) evidenceTimes.set(a.activity_id, a.event_time);
    for (const a of ctx.dataset.audit) evidenceTimes.set(a.audit_id, a.event_time);
  }
  return evidenceTimes.get(id);
}

export function validateCaseResult(ctx: AuditContext, req: DeletionRequest, r: Pick<CaseResult, 'case_id' | 'deletion_status' | 'steps'>): string[] {
  const p: string[] = [];
  if (!['COMPLETE', 'INCOMPLETE', 'NOT_APPLICABLE'].includes(r.deletion_status)) p.push(`bad status ${r.deletion_status}`);
  if (r.deletion_status === 'NOT_APPLICABLE' && r.steps.length) p.push('NOT_APPLICABLE must have []');
  let prevKey: [number, number, string, string] | null = null;
  r.steps.forEach((s, i) => {
    if (s.step_no !== i + 1) p.push(`step_no ${s.step_no} at position ${i + 1}`);
    const a = ALLOWED[s.operation];
    if (!a) p.push(`unknown operation ${s.operation}`);
    else if (a.phase !== s.phase || a.state !== s.state) p.push(`invalid ${s.phase}/${s.operation}/${s.state}`);
    const key: [number, number, string, string] = [PHASE_ORDER[s.phase], OPERATION_ORDER[s.operation], s.system_id, s.resource_id];
    if (prevKey && (key[0] < prevKey[0] || (key[0] === prevKey[0] && (key[1] < prevKey[1] || (key[1] === prevKey[1] && `${key[2]}|${key[3]}` < `${prevKey[2]}|${prevKey[3]}`)))))
      p.push(`ordering violated at step ${s.step_no}`);
    prevKey = key;
    const mustCite = s.phase === 'FINDING' || (r.deletion_status === 'COMPLETE' && s.phase === 'VERIFICATION');
    if (mustCite) {
      const t = evidenceTime(ctx, s.evidence_id);
      if (!s.evidence_id) p.push(`step ${s.step_no} missing evidence`);
      else if (!t) p.push(`step ${s.step_no} unknown evidence ${s.evidence_id}`);
      else if (iso(t) > iso(req.audit_at)) p.push(`step ${s.step_no} cites ${s.evidence_id} captured after audit_at`);
    } else if (s.evidence_id !== '') p.push(`step ${s.step_no} future work must have empty evidence_id`);
  });
  if (r.deletion_status === 'COMPLETE' && r.steps.some((s) => s.phase !== 'VERIFICATION')) p.push('COMPLETE has non-verification steps');
  if (r.deletion_status === 'INCOMPLETE' && !r.steps.some((s) => s.phase === 'FINDING')) p.push('INCOMPLETE without findings');
  return p;
}

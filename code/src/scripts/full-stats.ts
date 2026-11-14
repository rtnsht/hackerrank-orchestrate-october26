import { getAuditContext, auditCase } from '../core/pipeline.js';
const ctx = await getAuditContext({ log: console.error });
const counts: Record<string, number> = {};
const opCounts: Record<string, number> = {};
let noAccount = 0, warnings = 0, empty = 0;
const show = process.argv.slice(2);
for (const req of ctx.dataset.requests) {
  const r = auditCase(ctx, req);
  counts[r.deletion_status] = (counts[r.deletion_status] ?? 0) + 1;
  for (const s of r.steps) opCounts[s.operation] = (opCounts[s.operation] ?? 0) + 1;
  if (r.deletion_status !== 'NOT_APPLICABLE' && !r.steps.some((s) => s.system_id === 'account_service')) { noAccount++; if (show.includes('noacct')) console.log('NOACCT', req.case_id, req.email, req.reported_status, r.deletion_status, r.steps.length); }
  if (r.trace.warnings.length) { warnings++; if (show.includes('warn')) console.log('WARN', req.case_id, r.trace.warnings.join(' | ')); }
  if (r.deletion_status !== 'NOT_APPLICABLE' && r.steps.length === 0) empty++;
  if (show.includes(req.case_id)) console.log(JSON.stringify({ status: r.deletion_status, steps: r.steps, trace: r.trace }, null, 1));
}
console.log({ counts, opCounts, noAccount, warnings, empty });

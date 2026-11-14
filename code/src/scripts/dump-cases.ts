import fs from 'node:fs';
import { getAuditContext, auditCase } from '../core/pipeline.js';
const [out, ...ids] = process.argv.slice(2);
const ctx = await getAuditContext();
const res: Record<string, unknown> = {};
for (const req of [...ctx.dataset.sampleRequests, ...ctx.dataset.requests]) {
  if (!ids.includes(req.case_id)) continue;
  const r = auditCase(ctx, req);
  res[req.case_id] = { request: req, deletion_status: r.deletion_status, steps: r.steps, assessments: r.trace.assessments, warnings: r.trace.warnings };
}
fs.writeFileSync(out, JSON.stringify(res, null, 1));
console.log('wrote', Object.keys(res).length);

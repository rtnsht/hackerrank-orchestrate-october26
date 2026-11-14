import fs from 'node:fs';
import path from 'node:path';
import { getAuditContext, auditCase } from '../core/pipeline.js';
import { parseCsv } from '../core/csv.js';
import { score } from '../../evaluation/score.js';
const ctx = await getAuditContext({ log: console.error });
const gold = parseCsv(fs.readFileSync(path.join(ctx.dataset.dir, 'sample_output.csv'), 'utf8'));
const goldMap = new Map(gold.map((g) => [g.case_id, g]));
const rows = ctx.dataset.sampleRequests.map((req) => {
  const r = auditCase(ctx, req);
  const g = goldMap.get(req.case_id)!;
  return { case_id: req.case_id, predStatus: r.deletion_status, predSteps: r.steps, goldStatus: g.deletion_status, goldSteps: JSON.parse(g.steps_json), trace: r.trace };
});
const m = score(rows);
const { perCase, ...summary } = m;
console.log(JSON.stringify(summary, null, 1));
const only = process.argv.slice(2);
for (const pc of perCase) {
  if (pc.exact && !only.includes(pc.case_id)) continue;
  if (only.length && !only.includes(pc.case_id)) continue;
  const row = rows.find((r) => r.case_id === pc.case_id)!;
  console.log(`\n### ${pc.case_id} pred=${row.predStatus} gold=${row.goldStatus} tp=${pc.tp} fp=${pc.fp} fn=${pc.fn}`);
  for (const x of pc.missing) console.log('   MISSING', x);
  for (const x of pc.extra) console.log('   EXTRA  ', x);
  if (only.length) console.log(JSON.stringify(row.trace, null, 1));
}

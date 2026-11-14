import { loadDataset, CODE_ROOT } from '../core/dataset.js';
import { buildEvidenceIndex } from '../core/evidence/index.js';
const ds = loadDataset();
const idx = await buildEvidenceIndex({ datasetDir: ds.dir, codeRoot: CODE_ROOT, manifest: ds.manifest, activity: ds.activity, log: console.error });
console.log(idx.stats, 'obs', idx.observations.length, 'scopes', idx.scopes.length);
for (const r of process.argv.slice(2)) {
  for (const o of idx.byResource.get(r) ?? []) console.log(r, o.evidenceId, o.observedAt, o.system, o.category, o.state, o.claim ? 'CLAIM' : '', o.copy ?? '', o.parser, o.note ?? '', o.identifiers.join(' '));
}

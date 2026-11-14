// Scoring utilities: compares predicted rows with reference rows (sample_output.csv or custom cases).
import type { Step } from '../src/core/types.js';

export interface RowPair { case_id: string; predStatus: string; predSteps: Step[]; goldStatus: string; goldSteps: Step[] }

const FIELDS: (keyof Step)[] = ['step_no', 'phase', 'system_id', 'resource_id', 'operation', 'state', 'evidence_id'];
const key = (s: Step) => `${s.phase}|${s.system_id}|${s.resource_id}|${s.operation}|${s.state}|${s.evidence_id}`;
const keyNoEv = (s: Step) => `${s.phase}|${s.system_id}|${s.resource_id}|${s.operation}|${s.state}`;

export interface Metrics {
  cases: number;
  statusAccuracy: number;
  exactCaseMatch: number;
  stepPrecision: number;
  stepRecall: number;
  stepF1: number;
  stepF1IgnoringEvidence: number;
  fieldAccuracy: Record<string, number>;
  sequenceAccuracy: number;
  perCase: { case_id: string; statusOk: boolean; exact: boolean; tp: number; fp: number; fn: number; missing: string[]; extra: string[] }[];
}

function f1(p: number, r: number) { return p + r === 0 ? 0 : (2 * p * r) / (p + r); }

export function score(rows: RowPair[]): Metrics {
  let statusOk = 0, exact = 0, tp = 0, fp = 0, fn = 0, tpNe = 0, fpNe = 0, fnNe = 0, seqOk = 0;
  const fieldHits: Record<string, number> = Object.fromEntries(FIELDS.map((f) => [f, 0]));
  let fieldTotal = 0;
  const perCase: Metrics['perCase'] = [];
  for (const r of rows) {
    const sOk = r.predStatus === r.goldStatus;
    if (sOk) statusOk++;
    const g = r.goldSteps.map(key), p = r.predSteps.map(key);
    const gs = new Set(g), ps = new Set(p);
    const ctp = p.filter((k) => gs.has(k)).length;
    tp += ctp; fp += p.length - ctp; fn += g.length - ctp;
    const gn = new Set(r.goldSteps.map(keyNoEv));
    const pn = r.predSteps.map(keyNoEv);
    const ctpN = pn.filter((k) => gn.has(k)).length;
    tpNe += ctpN; fpNe += pn.length - ctpN; fnNe += gn.size - ctpN;
    const isExact = sOk && JSON.stringify(g) === JSON.stringify(p);
    if (isExact) exact++;
    // sequence: the relative order of steps present in both lists matches
    const common = p.filter((k) => gs.has(k));
    const goldOrder = g.filter((k) => ps.has(k));
    if (JSON.stringify(common) === JSON.stringify(goldOrder) && g.length === p.length && sOk) seqOk++;
    // field-level: positional comparison over max length
    const n = Math.max(r.goldSteps.length, r.predSteps.length);
    for (let i = 0; i < n; i++) {
      fieldTotal++;
      for (const f of FIELDS) if (r.goldSteps[i] && r.predSteps[i] && r.goldSteps[i][f] === r.predSteps[i][f]) fieldHits[f]++;
    }
    perCase.push({ case_id: r.case_id, statusOk: sOk, exact: isExact, tp: ctp, fp: p.length - ctp, fn: g.length - ctp,
      missing: g.filter((k) => !ps.has(k)), extra: p.filter((k) => !gs.has(k)) });
  }
  const P = tp / Math.max(1, tp + fp), R = tp / Math.max(1, tp + fn);
  const Pn = tpNe / Math.max(1, tpNe + fpNe), Rn = tpNe / Math.max(1, tpNe + fnNe);
  return {
    cases: rows.length,
    statusAccuracy: statusOk / Math.max(1, rows.length),
    exactCaseMatch: exact / Math.max(1, rows.length),
    stepPrecision: P, stepRecall: R, stepF1: f1(P, R),
    stepF1IgnoringEvidence: f1(Pn, Rn),
    fieldAccuracy: Object.fromEntries(FIELDS.map((f) => [f, fieldHits[f] / Math.max(1, fieldTotal)])),
    sequenceAccuracy: seqOk / Math.max(1, rows.length),
    perCase,
  };
}

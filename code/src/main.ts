// CLI: run the Mastra deletion-audit workflow for every request and write output.csv.
//
//   npm start                         -> dataset/deletion_requests.csv -> <repo>/output.csv (+ usage report)
//   npm start -- --samples            -> sample requests, scored against dataset/sample_output.csv
//   npm start -- --input X --output Y -> custom files
import { loadedEnvFile } from './env.js'; // must stay first: loads code/.env before agents are configured
import fs from 'node:fs';
import path from 'node:path';
import { parseCsv, toCsv } from './core/csv.js';
import { CODE_ROOT, REPO_ROOT } from './core/dataset.js';
import { getAuditContext } from './core/pipeline.js';
import type { DeletionRequest, Step } from './core/types.js';
import { extractMediaWithAgent } from './mastra/agents/index.js';
import { mastra } from './mastra/index.js';
import { describeAgentConfig, usageTracker } from './mastra/llm.js';
import { validateCaseResult } from './core/validate.js';
import { writeUsageReport } from './report/usage-report.js';

interface Args {
  input?: string;
  output: string;
  samples: boolean;
  concurrency: number;
  trace?: string;
  usageReport: boolean;
}

function parseArgs(argv: string[]): Args {
  const a: Args = { output: path.join(REPO_ROOT, 'output.csv'), samples: false, concurrency: 8, usageReport: true };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    if (k === '--input') a.input = argv[++i];
    else if (k === '--output') a.output = argv[++i];
    else if (k === '--samples') a.samples = true;
    else if (k === '--concurrency') a.concurrency = Number(argv[++i]);
    else if (k === '--trace') a.trace = argv[++i];
    else if (k === '--no-usage-report') a.usageReport = false;
  }
  if (a.samples && !argv.includes('--output')) a.output = path.join(CODE_ROOT, '.runs', 'sample_predictions.csv');
  return a;
}

export function caseOrder(a: string, b: string): number {
  const na = Number(a.replace(/\D+/g, ''));
  const nb = Number(b.replace(/\D+/g, ''));
  return na - nb || a.localeCompare(b);
}

async function mapPool<T, R>(items: T[], n: number, fn: (t: T) => Promise<R>): Promise<R[]> {
  const out: R[] = new Array(items.length);
  let next = 0;
  await Promise.all(
    Array.from({ length: Math.max(1, n) }, async () => {
      while (next < items.length) {
        const i = next++;
        out[i] = await fn(items[i]);
      }
    }),
  );
  return out;
}

export interface RunOutput {
  rows: { case_id: string; deletion_status: string; steps: Step[]; problems: string[]; trace: unknown }[];
  seconds: number;
}

export async function runAudit(requests: DeletionRequest[], concurrency = 8): Promise<RunOutput> {
  const t0 = Date.now();
  await getAuditContext({ mediaExtractor: extractMediaWithAgent, log: (m) => console.error(`[evidence] ${m}`) });
  const workflow = mastra.getWorkflow('deletionAuditWorkflow');
  const rows = await mapPool(requests, concurrency, async (req) => {
    const run = await workflow.createRun();
    const res = await run.start({ inputData: req });
    if (res.status !== 'success') {
      return { case_id: req.case_id, deletion_status: 'COMPLETE', steps: [], problems: [`workflow ${res.status}`], trace: res };
    }
    return res.result as RunOutput['rows'][number];
  });
  rows.sort((a, b) => caseOrder(a.case_id, b.case_id));
  return { rows, seconds: (Date.now() - t0) / 1000 };
}

const PROCESS_START = Date.now();

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const ctx = await getAuditContext({ mediaExtractor: extractMediaWithAgent, log: (m) => console.error(`[evidence] ${m}`) });
  const requests = args.input
    ? (parseCsv(fs.readFileSync(args.input, 'utf8')) as unknown as DeletionRequest[])
    : args.samples
      ? ctx.dataset.sampleRequests
      : ctx.dataset.requests;
  console.error(`agent config${loadedEnvFile ? ` (from ${path.relative(process.cwd(), loadedEnvFile) || loadedEnvFile})` : ' (no .env file; environment only)'}:`);
  for (const line of describeAgentConfig()) console.error(`  - ${line}`);
  console.error(`auditing ${requests.length} requests with the Mastra deletion-audit workflow...`);
  const { rows } = await runAudit(requests, args.concurrency);
  const seconds = (Date.now() - PROCESS_START) / 1000; // includes evidence loading / OCR

  // contract check for every row (NOT_APPLICABLE included), on top of the per-step check inside plan-deletion-steps
  const byId = new Map(requests.map((q) => [q.case_id, q]));
  const problems = rows.flatMap((r) => {
    const req = byId.get(r.case_id);
    const all = new Set([...r.problems, ...(req ? validateCaseResult(ctx, req, { case_id: r.case_id, deletion_status: r.deletion_status as never, steps: r.steps }) : ['unknown case_id'])]);
    return [...all].map((p) => `${r.case_id}: ${p}`);
  });
  const ids = new Set(rows.map((r) => r.case_id));
  if (ids.size !== requests.length) problems.push(`expected ${requests.length} unique rows, got ${ids.size}`);
  fs.mkdirSync(path.dirname(args.output), { recursive: true });
  fs.writeFileSync(
    args.output,
    toCsv(
      ['case_id', 'deletion_status', 'steps_json'],
      rows.map((r) => ({ case_id: r.case_id, deletion_status: r.deletion_status, steps_json: JSON.stringify(r.steps) })),
    ),
  );
  if (args.trace) {
    fs.mkdirSync(path.dirname(args.trace), { recursive: true });
    fs.writeFileSync(args.trace, rows.map((r) => JSON.stringify({ case_id: r.case_id, deletion_status: r.deletion_status, trace: r.trace })).join('\n') + '\n');
  }
  const counts = rows.reduce<Record<string, number>>((m, r) => ((m[r.deletion_status] = (m[r.deletion_status] ?? 0) + 1), m), {});
  console.error(`wrote ${rows.length} rows to ${args.output} in ${seconds.toFixed(1)}s`, counts);
  if (args.usageReport && !args.samples && !args.input) {
    writeUsageReport({
      file: path.join(CODE_ROOT, 'evaluation', 'usage_report.md'),
      requests: requests.length,
      seconds,
      usage: usageTracker.snapshot(),
      stats: ctx.index.stats,
      counts,
    });
    console.error('updated evaluation/usage_report.md');
  }
  if (problems.length) {
    console.error(`CONTRACT PROBLEMS (${problems.length}):\n${problems.slice(0, 50).join('\n')}`);
    process.exitCode = 1;
  }
}

const isEntry = process.argv[1] && path.resolve(process.argv[1]) === path.resolve(new URL(import.meta.url).pathname);
if (isEntry) {
  main().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}

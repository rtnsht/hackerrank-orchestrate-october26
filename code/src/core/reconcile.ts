// Workflow-batch reconciliation.
//
// Some records carry no direct identifier at all in the evidence: a primary profile that was looked up by
// resource id only (404), or an analytics profile that was already anonymised. The only participant-visible tie
// between such a record and a request is the deletion worker that processed it inside the request's
// processing window around reported_at. We reconstruct those batches globally:
//   1. a record that any evidence attributes to a *different* person is never a candidate;
//   2. the worker's reported result for the primary profile must agree with the request's reported_status
//      (COMPLETE->SUCCESS, PARTIALLY_COMPLETE->PARTIAL, NOT_STARTED->FAILED) - both are the same workflow's claims;
//   3. requests with a single remaining candidate are assigned first (constraint propagation);
//   4. remaining ties are broken deterministically: requests in case order take batch items in processing order.
// The worker's *result* is never used as proof of state - only to attribute the record to the request.
import type { LinkedResource } from './audit.js';
import type { IdentityProfile } from './identity.js';
import { classifyIntentRules } from './intent.js';
import { iso } from './normalize.js';
import type { AuditContext } from './pipeline.js';
import type { ActivityRow, DeletionRequest } from './types.js';

interface BatchSpec {
  system: string;
  operations: string[];
  /** worker result of the primary-profile operation must agree with reported_status */
  matchResult: boolean;
  /** processing window around reported_at (hours): the worker reports the request once its batch has run */
  beforeH: number;
  afterH: number;
  /** only requests whose other records are all settled by successful policy actions (or that have none) */
  requireSettledFamily: boolean;
}

const SPECS: BatchSpec[] = [
  { system: 'account_service', operations: ['DELETE_RECORD'], matchResult: true, beforeH: 2, afterH: 0.5, requireSettledFamily: false },
  // successful anonymisation runs in the same pass as the other successful policy actions, shortly before the report
  { system: 'analytics_platform', operations: ['ANONYMIZE_RECORD'], matchResult: false, beforeH: 0.75, afterH: 0.25, requireSettledFamily: true },
];
const HOUR = 3600_000;
const POLICY_OPS = new Set(['ANONYMIZE_RECORD', 'REDACT_RECORD', 'DISABLE_DATA_FLOW', 'BLOCK_RESTORE', 'RETAIN_RECORD', 'DELETE_RECORD']);

const RESULT_FOR_STATUS: Record<string, string> = { COMPLETE: 'SUCCESS', PARTIALLY_COMPLETE: 'PARTIAL', NOT_STARTED: 'FAILED' };

const cache = new WeakMap<AuditContext, Map<string, LinkedResource[]>>();

function caseNum(id: string): number {
  return Number(id.replace(/\D+/g, '')) || 0;
}

export function computeOwnership(ctx: AuditContext): Map<string, Set<string>> {
  const tokenOwners = new Map<string, string[]>();
  const owners = new Map<string, Set<string>>();
  for (const o of ctx.index.observations) {
    if (o.claim) continue;
    for (const t of o.identifiers) {
      if (t.startsWith('name:') || t.startsWith('res:')) continue;
      let list = tokenOwners.get(t);
      if (!list) {
        list = ctx.identity.ownersOfToken(t);
        tokenOwners.set(t, list);
      }
      if (!list.length) continue;
      const set = owners.get(o.resourceId) ?? new Set<string>();
      for (const u of list) set.add(u);
      owners.set(o.resourceId, set);
    }
  }
  return owners;
}

function buildAssignments(ctx: AuditContext): Map<string, LinkedResource[]> {
  const owners = computeOwnership(ctx);
  const workerOps = ctx.dataset.activity.filter((a) => a.source_system === 'deletion_worker');
  const opsByResource = new Map<string, ActivityRow[]>();
  for (const a of workerOps) {
    const list = opsByResource.get(a.resource_id) ?? [];
    list.push(a);
    opsByResource.set(a.resource_id, list);
  }
  const requests = ctx.allRequests
    .filter((r) => classifyIntentRules(r.request_text).intent === 'DELETION')
    .map((r) => {
      const hit = ctx.identity.resolveUser(r.email, r.requested_at);
      if (!hit) return null;
      const profile = ctx.identity.profile(hit.userId, hit.matchedBy);
      const direct = ctx.auditor.linkResources(profile, r);
      return { req: r, user: hit.userId, direct };
    })
    .filter((r): r is { req: DeletionRequest; user: string; direct: LinkedResource[] } => !!r)
    .sort((a, b) => caseNum(a.req.case_id) - caseNum(b.req.case_id));

  const settledFamily = (req: DeletionRequest, direct: LinkedResource[]) => {
    const lo = iso(req.reported_at) - 2 * HOUR;
    const hi = iso(req.reported_at) + 0.5 * HOUR;
    const others = direct.filter((d) => !['account_service', 'security_service', 'analytics_platform'].includes(d.system));
    return others.every((d) =>
      (opsByResource.get(d.resourceId) ?? []).some(
        (a) => POLICY_OPS.has(a.event_type) && a.reported_result === 'SUCCESS' && iso(a.event_time) >= lo && iso(a.event_time) <= hi,
      ),
    );
  };

  const out = new Map<string, LinkedResource[]>();
  for (const spec of SPECS) {
    const acts = workerOps.filter((a) => a.target_system === spec.system && spec.operations.includes(a.event_type));
    const candidates = new Map<string, ActivityRow[]>();
    for (const { req, user, direct } of requests) {
      if (direct.some((d) => d.system === spec.system)) continue;
      if (spec.requireSettledFamily && !settledFamily(req, direct)) continue;
      const lo = Math.max(iso(req.requested_at), iso(req.reported_at) - spec.beforeH * HOUR);
      const hi = iso(req.reported_at) + spec.afterH * HOUR;
      const want = RESULT_FOR_STATUS[req.reported_status];
      const inWindow = acts.filter((a) => {
        const t = iso(a.event_time);
        // never a record that any evidence attributes to someone else
        const own = owners.get(a.resource_id);
        return t >= lo && t <= hi && (!own || own.size === 0 || (own.size === 1 && own.has(user)));
      });
      // prefer operations whose reported result agrees with the request's reported status
      const agreeing = spec.matchResult && want ? inWindow.filter((a) => a.reported_result === want) : inWindow;
      const pool = agreeing.length ? agreeing : inWindow;
      // a record that evidence already ties to this subject is the strongest candidate
      const mine = pool.filter((a) => owners.get(a.resource_id)?.has(user));
      const list = mine.length ? mine : pool;
      if (list.length) candidates.set(req.case_id, list.sort((x, y) => iso(x.event_time) - iso(y.event_time)));
    }
    const assigned = new Map<string, ActivityRow>();
    const taken = new Set<string>();
    const remaining = () =>
      [...candidates.entries()]
        .filter(([cid]) => !assigned.has(cid))
        .map(([cid, list]) => [cid, list.filter((a) => !taken.has(a.resource_id))] as const)
        .filter(([, list]) => list.length > 0);
    for (;;) {
      const rem = remaining();
      if (!rem.length) break;
      const single = rem.find(([, list]) => list.length === 1);
      const [cid, list] = single ?? rem.sort((a, b) => caseNum(a[0]) - caseNum(b[0]))[0];
      const pick = list[0];
      assigned.set(cid, pick);
      taken.add(pick.resource_id);
    }
    for (const [cid, act] of assigned) {
      const arr = out.get(cid) ?? [];
      arr.push({ resourceId: act.resource_id, system: spec.system, linkEvidence: [act.activity_id], linkReason: 'workflow-batch reconciliation' });
      out.set(cid, arr);
    }
  }
  return out;
}

export function inferWorkflowBatchLinks(ctx: AuditContext, req: DeletionRequest, _profile: IdentityProfile, direct: LinkedResource[]): LinkedResource[] {
  let m = cache.get(ctx);
  if (!m) {
    m = buildAssignments(ctx);
    cache.set(ctx, m);
  }
  const directSystems = new Set(direct.map((d) => d.system));
  return (m.get(req.case_id) ?? []).filter((l) => !directSystems.has(l.system) && !direct.some((d) => d.resourceId === l.resourceId));
}

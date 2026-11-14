// Deterministic audit pipeline shared by the Mastra tools/workflow and the CLI.
import { DeletionAuditor, type LinkedResource, type ResourceAssessment } from './audit.js';
import { CODE_ROOT, loadDataset, type Dataset } from './dataset.js';
import { buildEvidenceIndex, type EvidenceIndex, type MediaExtractor } from './evidence/index.js';
import { IdentityResolver, type IdentityProfile } from './identity.js';
import { classifyIntentRules, type IntentResult } from './intent.js';
import { inferWorkflowBatchLinks } from './reconcile.js';
import { DeletionPolicy } from './policy.js';
import type { CaseResult, DeletionRequest } from './types.js';
import { iso } from './normalize.js';

export interface AuditContext {
  dataset: Dataset;
  index: EvidenceIndex;
  identity: IdentityResolver;
  policy: DeletionPolicy;
  auditor: DeletionAuditor;
  allRequests: DeletionRequest[];
}

let ctxPromise: Promise<AuditContext> | null = null;

export function getAuditContext(opts: { mediaExtractor?: MediaExtractor; log?: (m: string) => void } = {}): Promise<AuditContext> {
  if (!ctxPromise) {
    ctxPromise = (async () => {
      const dataset = loadDataset();
      const index = await buildEvidenceIndex({
        datasetDir: dataset.dir,
        codeRoot: CODE_ROOT,
        manifest: dataset.manifest,
        activity: dataset.activity,
        mediaExtractor: opts.mediaExtractor,
        log: opts.log,
      });
      const identity = new IdentityResolver(dataset.identities, dataset.audit);
      const policy = new DeletionPolicy(dataset.rules);
      const auditor = new DeletionAuditor(index, policy);
      // both request files describe the same workflow batches, so both inform batch reconciliation
      const allRequests = [...dataset.sampleRequests, ...dataset.requests];
      return { dataset, index, identity, policy, auditor, allRequests };
    })();
  }
  return ctxPromise;
}

export interface CaseTrace {
  intent: IntentResult;
  userId?: string;
  matchedBy?: string;
  identifiers?: string[];
  linked?: LinkedResource[];
  assessments?: Pick<ResourceAssessment, 'resourceId' | 'system' | 'category' | 'treatment' | 'ruleId' | 'state' | 'stateEvidence' | 'stateAt' | 'compliant' | 'linkReason' | 'linkEvidence' | 'notes'>[];
  warnings: string[];
}

export function resolveProfile(ctx: AuditContext, req: DeletionRequest): IdentityProfile | null {
  const hit = ctx.identity.resolveUser(req.email, req.requested_at);
  return hit ? ctx.identity.profile(hit.userId, hit.matchedBy) : null;
}

/** True when the same person filed another deletion request after this case's audit_at. */
export function hasLaterRequest(ctx: AuditContext, req: DeletionRequest, userId: string): boolean {
  return ctx.allRequests.some(
    (r) => r.case_id !== req.case_id && iso(r.requested_at) > iso(req.audit_at) && classifyIntentRules(r.request_text).intent === 'DELETION' && ctx.identity.resolveUser(r.email, r.requested_at)?.userId === userId,
  );
}

/** Stage 3: every record tied to the subject (identifier links, source references, batch reconciliation). */
export function discoverResources(ctx: AuditContext, req: DeletionRequest, profile: IdentityProfile): LinkedResource[] {
  const laterLinks = !hasLaterRequest(ctx, req, profile.userId);
  const direct = ctx.auditor.linkResources(profile, req, [], { laterLinks });
  const inferred = inferWorkflowBatchLinks(ctx, req, profile, direct);
  return ctx.auditor.linkResources(profile, req, inferred, { laterLinks });
}

export function auditCase(ctx: AuditContext, req: DeletionRequest, intent: IntentResult = classifyIntentRules(req.request_text)): CaseResult & { trace: CaseTrace } {
  const trace: CaseTrace = { intent, warnings: [] };
  if (intent.intent !== 'DELETION') {
    return { case_id: req.case_id, deletion_status: 'NOT_APPLICABLE', steps: [], trace };
  }
  const profile = resolveProfile(ctx, req);
  if (!profile) {
    trace.warnings.push('request email does not resolve to a known user; no resources can be attributed');
    return { case_id: req.case_id, deletion_status: 'COMPLETE', steps: [], trace };
  }
  trace.userId = profile.userId;
  trace.matchedBy = profile.matchedBy;
  trace.identifiers = [...profile.tokens];
  const linked = discoverResources(ctx, req, profile);
  trace.linked = linked;
  const assessments = linked.map((l) => ctx.auditor.assess(l, req));
  for (const a of assessments) if (!a.plan) trace.warnings.push(`${a.resourceId}: ${a.notes.join('; ') || 'no applicable policy/state'}`);
  trace.assessments = assessments.map((a) => ({
    resourceId: a.resourceId,
    system: a.system,
    category: a.category,
    treatment: a.treatment,
    ruleId: a.ruleId,
    state: a.state,
    stateEvidence: a.stateEvidence,
    stateAt: a.stateAt,
    compliant: a.compliant,
    linkReason: a.linkReason,
    linkEvidence: a.linkEvidence,
    notes: a.notes,
  }));
  const { status, steps } = ctx.auditor.buildSteps(assessments);
  return { case_id: req.case_id, deletion_status: status, steps, trace };
}

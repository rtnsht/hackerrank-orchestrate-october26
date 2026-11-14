// Per-case deletion audit: link resources to the subject, reconstruct state at audit_at, apply policy, plan steps.
import type { EvidenceIndex } from './evidence/index.js';
import { systemFromResource } from './evidence/records.js';
import type { IdentityProfile } from './identity.js';
import { iso } from './normalize.js';
import { DeletionPolicy, OPERATION_ORDER, PHASE_ORDER, planFor, requiredVerification, type Treatment } from './policy.js';
import type { CaseResult, DeletionRequest, Observation, ObservedState, Phase, SnapshotScope, Step } from './types.js';

export interface LinkedResource {
  resourceId: string;
  system: string;
  linkEvidence: string[]; // evidence ids that tie the resource to the subject
  linkReason: string;
}

export interface ResourceAssessment extends LinkedResource {
  category?: string;
  treatment?: Treatment;
  ruleId?: string;
  state?: ObservedState;
  stateEvidence?: string;
  stateAt?: string;
  compliant?: boolean;
  plan?: ReturnType<typeof planFor>;
  /** in scope but no citeable state at audit_at: only the required final-state check is planned */
  unverified?: boolean;
  notes: string[];
}

const ABSENCE_CATEGORIES = new Set(['USER_PROFILE', 'SEARCH_INDEX_ENTRY', 'USER_EXPORT', 'CACHE_ENTRY', 'SUPPORT_ATTACHMENT', 'MARKETING_PROFILE']);

export interface AuditOptions {
  /** evidence captured after audit_at is ignored (strict point-in-time reconstruction) */
  strictCutoff: boolean;
}

export class DeletionAuditor {
  constructor(
    private index: EvidenceIndex,
    private policy: DeletionPolicy,
    private opts: AuditOptions = { strictCutoff: true },
  ) {}

  /** Information that existed at audit_at: record-level time decides (an export captured later can still show
   *  events that happened before the cut-off). Citations additionally require the file itself to predate audit_at. */
  private visible(o: Observation, auditAt: string): boolean {
    return iso(o.observedAt) <= iso(auditAt);
  }

  private citeable(o: Observation, auditAt: string): boolean {
    return iso(o.capturedAt) <= iso(auditAt);
  }

  /** Resources whose visible evidence carries any identifier of the subject (plus transitive source links). */
  linkResources(profile: IdentityProfile, req: DeletionRequest, extraLinks: LinkedResource[] = [], opts: { laterLinks?: boolean } = {}): LinkedResource[] {
    const linked = new Map<string, LinkedResource>();
    // A record joins this case if the evidence tying it to the subject is dated at or before audit_at (record-level
    // time, so a later export of earlier events still counts). With `laterLinks` (only used when the person has no
    // later deletion request that would own that evidence) later readbacks may also attribute records; such records
    // can never raise findings - their state is still judged only from evidence available at audit_at.
    for (const o of this.index.observations) {
      if (o.claim) continue;
      const later = !this.visible(o, req.audit_at);
      if (later && !opts.laterLinks) continue;
      const hit = o.identifiers.find((t) => profile.tokens.has(t));
      if (!hit) continue;
      const cur = linked.get(o.resourceId) ?? { resourceId: o.resourceId, system: o.system, linkEvidence: [], linkReason: later ? `post-audit attribution (${hit})` : hit };
      // a link available at audit_at always takes precedence over a post-audit attribution
      if (!later && cur.linkReason.startsWith('post-audit attribution')) cur.linkReason = hit;
      if (!cur.linkEvidence.includes(o.evidenceId)) cur.linkEvidence.push(o.evidenceId);
      linked.set(o.resourceId, cur);
    }
    for (const l of extraLinks) if (!linked.has(l.resourceId)) linked.set(l.resourceId, l);
    // transitive, both directions: a document that points at a linked source record (search document -> account),
    // and the source record named by a linked document.
    let grew = true;
    while (grew) {
      grew = false;
      for (const o of this.index.observations) {
        if (o.claim || !this.visible(o, req.audit_at)) continue;
        const refs = o.identifiers.filter((t) => t.startsWith('res:')).map((t) => t.slice(4));
        if (!refs.length) continue;
        if (!linked.has(o.resourceId)) {
          const src = refs.find((r) => linked.has(r));
          if (src) {
            linked.set(o.resourceId, { resourceId: o.resourceId, system: o.system, linkEvidence: [o.evidenceId], linkReason: `res:${src}` });
            grew = true;
          }
        } else {
          for (const r of refs) {
            if (linked.has(r)) continue;
            linked.set(r, { resourceId: r, system: systemFromResource(r) ?? 'unknown', linkEvidence: [o.evidenceId], linkReason: `source-of:${o.resourceId}` });
            grew = true;
          }
        }
      }
    }
    return [...linked.values()];
  }

  /** Point-in-time state of one resource at audit_at. */
  assess(link: LinkedResource, req: DeletionRequest): ResourceAssessment {
    const all = (this.index.byResource.get(link.resourceId) ?? []).filter((o) => this.visible(o, req.audit_at));
    // The audit judges the outcome of this request: sightings from before the request was submitted describe the
    // pre-deletion state (they still help identity linking, but never decide the final state).
    // Only evidence items that existed at audit_at (file captured by then) can establish - and be cited for - the
    // state. Later exports still help attribute records to the subject (linkResources) but never decide the state.
    const postRequest = all.filter((o) => !o.claim && iso(o.observedAt) >= iso(req.requested_at));
    const explicit = postRequest.filter((o) => o.state && this.citeable(o, req.audit_at));
    const notes: string[] = [];
    // category: prefer the most specific category seen in any visible evidence
    const cats = all.map((o) => o.category).filter((c): c is string => !!c);
    let category: string | undefined = cats.includes('SUPPORT_ATTACHMENT')
      ? 'SUPPORT_ATTACHMENT'
      : cats.includes('SUPPORT_CONTACT_RECORD')
        ? 'SUPPORT_CONTACT_RECORD'
        : cats.includes('SECONDARY_IDENTIFIER')
          ? 'SECONDARY_IDENTIFIER'
          : cats[cats.length - 1];
    const system = explicit[0]?.system ?? link.system ?? systemFromResource(link.resourceId) ?? 'unknown';
    if (!category) category = defaultCategory(system);

    explicit.sort((a, b) => iso(a.observedAt) - iso(b.observedAt) || iso(a.capturedAt) - iso(b.capturedAt) || severity(a.state) - severity(b.state));
    let latest: Observation | undefined = explicit[explicit.length - 1];
    // Same-instant observations from one snapshot (primary vs replica): any remaining copy counts.
    if (latest) {
      const same = explicit.filter((o) => o.evidenceId === latest!.evidenceId && o.observedAt === latest!.observedAt);
      const worst = same.sort((a, b) => severity(b.state) - severity(a.state))[0];
      if (worst) latest = worst;
    }
    let state = latest?.state;
    let stateEvidence = latest?.evidenceId;
    let stateAt = latest?.observedAt;

    // A record that was already gone before this request was submitted (and never seen again) belongs to an
    // earlier deletion, not to this request's scope.
    if (!latest) {
      const before = all.filter((o) => !o.claim && o.state && iso(o.observedAt) < iso(req.requested_at));
      before.sort((a, b) => iso(a.observedAt) - iso(b.observedAt));
      const prior = before[before.length - 1];
      if (prior && prior.state === 'ABSENT') {
        return { ...link, system, category, notes: [`already absent before the request (${prior.evidenceId} @ ${prior.observedAt})`] };
      }
    }

    // Absence proven by an enumerating snapshot. Snapshot bundles are often scoped to a subset of subjects, so an
    // omission never overrides an explicit post-request sighting; it only settles records with no such sighting.
    if (category && ABSENCE_CATEGORIES.has(category) && !latest) {
      // (Replication drift is handled by the rule above: a replica / failover copy seen after the request is an
      // explicit sighting, so a primary-only inventory that omits the record can never mask it.)
      const after = iso(req.reported_at);
      const usable = (s: SnapshotScope) =>
        s.system === system &&
        !s.resources.has(link.resourceId) &&
        iso(s.capturedAt) <= iso(req.audit_at) &&
        iso(s.fileCapturedAt ?? s.capturedAt) <= iso(req.audit_at);
      const candidates = this.index.scopes.filter((s) => usable(s) && iso(s.capturedAt) > after);
      if (candidates.length) {
        const best = pickScope(candidates);
        notes.push(`absent from enumerating snapshot ${best.evidenceId} (${best.system} @ ${best.capturedAt}, ${best.replicaCopies} copies)`);
        state = 'ABSENT';
        stateEvidence = best.evidenceId;
        stateAt = best.capturedAt;
      } else {
        // fall back to the latest enumerating snapshot before audit_at even if it predates the report
        const older = this.index.scopes.filter((s) => usable(s) && iso(s.capturedAt) >= iso(req.requested_at));
        if (older.length) {
          const best = pickScope(older);
          notes.push(`absent from snapshot ${best.evidenceId} captured before the report`);
          state = 'ABSENT';
          stateEvidence = best.evidenceId;
          stateAt = best.capturedAt;
        }
      }
    }

    const decision = this.policy.decide(category, system, req.audit_at, all.map((o) => o.note ?? '').join(' '));
    let plan = decision ? planFor(decision.treatment, state) : null;
    let unverified = false;
    if (!state) {
      notes.push('no citeable state evidence at audit_at');
      // The record is in scope (seen after the request, or processed by this request's worker batch) but nothing
      // available at audit_at proves its state: no finding can be raised, yet the post-remediation verification
      // must still cover its required final state.
      const inScope = postRequest.length > 0 || link.linkReason === 'workflow-batch reconciliation' || link.linkReason.startsWith('post-audit attribution');
      if (decision && inScope) {
        plan = { verification: requiredVerification(decision.treatment), compliant: true };
        unverified = true;
      }
    }
    return {
      ...link,
      system,
      category,
      treatment: decision?.treatment,
      ruleId: decision?.ruleId,
      state,
      stateEvidence,
      stateAt,
      compliant: plan?.compliant,
      plan,
      unverified,
      notes,
    };
  }

  /** Turn assessments into the ordered steps_json array. */
  buildSteps(assessments: ResourceAssessment[]): { status: 'COMPLETE' | 'INCOMPLETE'; steps: Step[] } {
    const planned = assessments.filter((a) => a.plan);
    const incomplete = planned.some((a) => !a.plan!.compliant);
    // A COMPLETE verdict may only contain evidence-backed verifications; unverified records are listed only as
    // future checks of an INCOMPLETE plan.
    const usable = planned.filter((a) => incomplete || !a.unverified);
    const raw: Omit<Step, 'step_no'>[] = [];
    for (const a of usable) {
      const p = a.plan!;
      if (incomplete) {
        if (p.finding) raw.push({ phase: 'FINDING', system_id: a.system, resource_id: a.resourceId, ...p.finding, evidence_id: a.stateEvidence ?? '' });
        if (p.remediation) raw.push({ phase: 'REMEDIATION', system_id: a.system, resource_id: a.resourceId, ...p.remediation, evidence_id: '' });
        raw.push({ phase: 'VERIFICATION', system_id: a.system, resource_id: a.resourceId, ...p.verification, evidence_id: '' });
      } else {
        raw.push({ phase: 'VERIFICATION', system_id: a.system, resource_id: a.resourceId, ...p.verification, evidence_id: a.stateEvidence ?? '' });
      }
    }
    raw.sort(
      (x, y) =>
        PHASE_ORDER[x.phase as Phase] - PHASE_ORDER[y.phase as Phase] ||
        OPERATION_ORDER[x.operation] - OPERATION_ORDER[y.operation] ||
        cmp(x.system_id, y.system_id) ||
        cmp(x.resource_id, y.resource_id),
    );
    return { status: incomplete ? 'INCOMPLETE' : 'COMPLETE', steps: raw.map((s, i) => ({ step_no: i + 1, ...s })) };
  }
}

function cmp(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

function severity(s: ObservedState | undefined): number {
  // higher = more exposure; used to keep a remaining copy when copies disagree at the same instant
  switch (s) {
    case 'IDENTIFIABLE':
    case 'PRESENT':
    case 'ACTIVE':
    case 'RESTORABLE':
      return 2;
    case 'REDACTED':
    case 'ANONYMIZED':
    case 'DISABLED':
    case 'BLOCKED':
      return 1;
    default:
      return 0;
  }
}

function pickScope(c: SnapshotScope[]): SnapshotScope {
  return [...c].sort((a, b) => b.replicaCopies - a.replicaCopies || iso(b.capturedAt) - iso(a.capturedAt))[0];
}

function defaultCategory(system: string): string | undefined {
  switch (system) {
    case 'account_service':
      return 'USER_PROFILE';
    case 'search_service':
      return 'SEARCH_INDEX_ENTRY';
    case 'analytics_platform':
      return 'ANALYTICS_PROFILE';
    case 'support_platform':
      return 'SUPPORT_MESSAGE';
    case 'export_service':
      return 'USER_EXPORT';
    case 'backup_service':
      return 'BACKUP_ARCHIVE';
    case 'integration_service':
      return 'DATA_FLOW';
    case 'security_service':
      return 'SECURITY_AUDIT_LOG';
    case 'identity_service':
      return 'ACCOUNT_TOMBSTONE';
    case 'legal_service':
      return 'LEGAL_HOLD_RECORD';
    case 'marketing_platform':
      return 'MARKETING_PROFILE';
    case 'object_storage':
      return 'SUPPORT_ATTACHMENT';
    case 'cache_service':
      return 'CACHE_ENTRY';
    case 'email_delivery_service':
      return 'EMAIL_DELIVERY_LOG';
    default:
      return undefined;
  }
}

export type { CaseResult };

// Mastra tools: each stage of the deletion audit exposed as a typed tool. The workflow composes them, and they can
// also be attached to an agent (e.g. in Mastra Studio) to explore a case interactively.
import { createTool } from '@mastra/core/tools';
import { z } from 'zod';
import type { LinkedResource } from '../../core/audit.js';
import { discoverResources, getAuditContext, resolveProfile } from '../../core/pipeline.js';
import { validateCaseResult } from '../../core/validate.js';
import type { DeletionRequest } from '../../core/types.js';
import { classifyIntent } from '../agents/index.js';

export const RequestSchema = z.object({
  case_id: z.string(),
  email: z.string(),
  requested_at: z.string(),
  reported_status: z.string(),
  reported_at: z.string(),
  audit_at: z.string(),
  request_text: z.string(),
});

export const IntentSchema = z.object({
  intent: z.enum(['DELETION', 'NON_DELETION']),
  confidence: z.number(),
  requestType: z.string(),
  rationale: z.string(),
  source: z.string(),
});

export const ProfileSchema = z.object({
  userId: z.string(),
  matchedBy: z.string(),
  identifiers: z.array(z.string()),
});

export const LinkedSchema = z.object({
  resourceId: z.string(),
  system: z.string(),
  linkEvidence: z.array(z.string()),
  linkReason: z.string(),
});

export const AssessmentSchema = z.object({
  resourceId: z.string(),
  system: z.string(),
  category: z.string().optional(),
  treatment: z.string().optional(),
  ruleId: z.string().optional(),
  state: z.string().optional(),
  stateEvidence: z.string().optional(),
  stateAt: z.string().optional(),
  compliant: z.boolean().optional(),
  linkReason: z.string(),
  linkEvidence: z.array(z.string()),
  notes: z.array(z.string()),
  plan: z.any().optional(),
  unverified: z.boolean().optional(),
});

export const StepSchema = z.object({
  step_no: z.number(),
  phase: z.enum(['FINDING', 'REMEDIATION', 'VERIFICATION']),
  system_id: z.string(),
  resource_id: z.string(),
  operation: z.string(),
  state: z.string(),
  evidence_id: z.string(),
});

export const classifyIntentTool = createTool({
  id: 'classify-request-intent',
  description: 'Decide whether a privacy request authorizes deletion (multilingual LLM agent with rule cross-check).',
  inputSchema: z.object({ request_text: z.string() }),
  outputSchema: IntentSchema,
  execute: async (input) => classifyIntent(input.request_text),
});

export const resolveIdentityTool = createTool({
  id: 'resolve-subject-identity',
  description: 'Resolve the request email (current or historical) to a user and list every identifier the user has ever used.',
  inputSchema: RequestSchema,
  outputSchema: z.object({ profile: ProfileSchema.nullable() }),
  execute: async (input) => {
    const ctx = await getAuditContext();
    const p = resolveProfile(ctx, input as DeletionRequest);
    return { profile: p ? { userId: p.userId, matchedBy: p.matchedBy, identifiers: [...p.tokens] } : null };
  },
});

export const discoverResourcesTool = createTool({
  id: 'discover-subject-resources',
  description:
    'Find every record tied to the subject: identifier matches across all evidence, trace correlation, catalog links, source-record references, and deletion-worker batch reconciliation for identifier-less records.',
  inputSchema: z.object({ request: RequestSchema, userId: z.string() }),
  outputSchema: z.object({ linked: z.array(LinkedSchema) }),
  execute: async (input) => {
    const ctx = await getAuditContext();
    const req = input.request as DeletionRequest;
    const profile = ctx.identity.profile(input.userId);
    const linked: LinkedResource[] = discoverResources(ctx, req, profile);
    return { linked };
  },
});

export const reconstructStateTool = createTool({
  id: 'reconstruct-resource-state',
  description: 'Reconstruct each linked record\'s state at audit_at from evidence (claims ignored) and apply deletion_rules.csv.',
  inputSchema: z.object({ request: RequestSchema, linked: z.array(LinkedSchema) }),
  outputSchema: z.object({ assessments: z.array(AssessmentSchema) }),
  execute: async (input) => {
    const ctx = await getAuditContext();
    const assessments = input.linked.map((l) => ctx.auditor.assess(l, input.request as DeletionRequest));
    return { assessments };
  },
});

export const planStepsTool = createTool({
  id: 'plan-deletion-steps',
  description: 'Turn assessments into the ordered FINDING / REMEDIATION / VERIFICATION steps and validate them against the output contract.',
  inputSchema: z.object({ request: RequestSchema, assessments: z.array(AssessmentSchema) }),
  outputSchema: z.object({ deletion_status: z.enum(['COMPLETE', 'INCOMPLETE']), steps: z.array(StepSchema), problems: z.array(z.string()) }),
  execute: async (input) => {
    const ctx = await getAuditContext();
    const { status, steps } = ctx.auditor.buildSteps(input.assessments as never);
    const problems = validateCaseResult(ctx, input.request as DeletionRequest, { case_id: input.request.case_id, deletion_status: status, steps });
    return { deletion_status: status, steps, problems };
  },
});

export const auditTools = {
  classifyIntentTool,
  resolveIdentityTool,
  discoverResourcesTool,
  reconstructStateTool,
  planStepsTool,
};

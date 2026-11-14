// The deletion-audit workflow: one run per request.
//
//   classify-intent -> resolve-identity -> discover-resources -> reconstruct-state -> plan-steps
//
// Non-deletion requests short-circuit to NOT_APPLICABLE with an empty plan. Each step delegates to a Mastra tool.
import { createStep, createWorkflow } from '@mastra/core/workflows';
import { z } from 'zod';
import { reviewAudit } from '../agents/index.js';
import {
  AssessmentSchema,
  IntentSchema,
  LinkedSchema,
  ProfileSchema,
  RequestSchema,
  StepSchema,
  classifyIntentTool,
  discoverResourcesTool,
  planStepsTool,
  reconstructStateTool,
  resolveIdentityTool,
} from '../tools/audit-tools.js';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const call = <I, O>(tool: any, input: I) => tool.execute(input, {}) as Promise<O>;

const CaseState = z.object({
  request: RequestSchema,
  intent: IntentSchema,
  profile: ProfileSchema.nullable().optional(),
  linked: z.array(LinkedSchema).optional(),
  assessments: z.array(AssessmentSchema).optional(),
});

const classifyStep = createStep({
  id: 'classify-intent',
  description: 'Does the request authorize deletion?',
  inputSchema: RequestSchema,
  outputSchema: CaseState,
  execute: async ({ inputData }) => {
    const intent = await call<{ request_text: string }, z.infer<typeof IntentSchema>>(classifyIntentTool, { request_text: inputData.request_text });
    return { request: inputData, intent };
  },
});

const identityStep = createStep({
  id: 'resolve-identity',
  description: 'Resolve current and historical identifiers of the subject.',
  inputSchema: CaseState,
  outputSchema: CaseState,
  execute: async ({ inputData }) => {
    if (inputData.intent.intent !== 'DELETION') return { ...inputData, profile: null };
    const { profile } = await call<typeof inputData.request, { profile: z.infer<typeof ProfileSchema> | null }>(resolveIdentityTool, inputData.request);
    return { ...inputData, profile };
  },
});

const discoverStep = createStep({
  id: 'discover-resources',
  description: 'Find every record, flow and backup tied to the subject.',
  inputSchema: CaseState,
  outputSchema: CaseState,
  execute: async ({ inputData }) => {
    if (!inputData.profile) return { ...inputData, linked: [] };
    const { linked } = await call<unknown, { linked: z.infer<typeof LinkedSchema>[] }>(discoverResourcesTool, { request: inputData.request, userId: inputData.profile.userId });
    return { ...inputData, linked };
  },
});

const stateStep = createStep({
  id: 'reconstruct-state',
  description: 'Reconstruct state at audit_at and apply the deletion policy.',
  inputSchema: CaseState,
  outputSchema: CaseState,
  execute: async ({ inputData }) => {
    if (!inputData.linked?.length) return { ...inputData, assessments: [] };
    const { assessments } = await call<unknown, { assessments: z.infer<typeof AssessmentSchema>[] }>(reconstructStateTool, { request: inputData.request, linked: inputData.linked });
    return { ...inputData, assessments };
  },
});

export const CaseOutputSchema = z.object({
  case_id: z.string(),
  deletion_status: z.enum(['COMPLETE', 'INCOMPLETE', 'NOT_APPLICABLE']),
  steps: z.array(StepSchema),
  problems: z.array(z.string()),
  trace: z.any(),
});

const planStep = createStep({
  id: 'plan-steps',
  description: 'Produce ordered findings, remediation and verification steps; validate the contract.',
  inputSchema: CaseState,
  outputSchema: CaseOutputSchema,
  execute: async ({ inputData }) => {
    const trace = {
      intent: inputData.intent,
      userId: inputData.profile?.userId,
      matchedBy: inputData.profile?.matchedBy,
      linked: inputData.linked,
      assessments: inputData.assessments?.map(({ plan: _plan, ...a }) => a),
    } as Record<string, unknown>;
    if (inputData.intent.intent !== 'DELETION') {
      return { case_id: inputData.request.case_id, deletion_status: 'NOT_APPLICABLE' as const, steps: [], problems: [], trace };
    }
    const planned = await call<unknown, { deletion_status: 'COMPLETE' | 'INCOMPLETE'; steps: z.infer<typeof StepSchema>[]; problems: string[] }>(planStepsTool, {
      request: inputData.request,
      assessments: inputData.assessments ?? [],
    });
    const review = await reviewAudit({ request: inputData.request, assessments: trace.assessments, steps: planned.steps });
    if (review) trace.review = review;
    return { case_id: inputData.request.case_id, ...planned, trace };
  },
});

export const deletionAuditWorkflow = createWorkflow({
  id: 'deletion-audit',
  description: 'Audit one deletion request end-to-end and return its output row.',
  inputSchema: RequestSchema,
  outputSchema: CaseOutputSchema,
})
  .then(classifyStep)
  .then(identityStep)
  .then(discoverStep)
  .then(stateStep)
  .then(planStep)
  .commit();

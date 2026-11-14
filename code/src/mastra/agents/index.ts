// Mastra agents. Prompts live in ../prompts/*.md so they can be reviewed without reading code.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Agent } from '@mastra/core/agent';
import { z } from 'zod';
import { classifyIntentRules, type IntentResult } from '../../core/intent.js';
import type { Observation, ObservedState } from '../../core/types.js';
import type { ParsedEvidence } from '../../core/evidence/structured.js';
import { normEmail, normPhone } from '../../core/normalize.js';
import { systemFromResource } from '../../core/evidence/records.js';
import { llmEnabled, modelFor, resolveModel, usageTracker } from '../llm.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const prompt = (name: string) => fs.readFileSync(path.join(here, '..', 'prompts', `${name}.md`), 'utf8');

export const intentAgent = new Agent({
  id: 'deletion-intent-agent',
  name: 'Deletion intent classifier',
  description: 'Decides whether a multilingual privacy request authorizes account / personal-data deletion.',
  instructions: prompt('intent-classifier'),
  model: () => resolveModel('intent') as never,
});

export const evidenceVisionAgent = new Agent({
  id: 'evidence-vision-agent',
  name: 'Evidence screenshot and document reader',
  description: 'Extracts record-level facts (resource, state, identifiers) from screenshots and PDFs.',
  instructions: prompt('evidence-vision'),
  model: () => resolveModel('vision') as never,
});

export const auditReviewerAgent = new Agent({
  id: 'audit-reviewer-agent',
  name: 'Deletion audit reviewer',
  description: 'Challenges a deterministic audit plan and records concerns for human review.',
  instructions: prompt('audit-reviewer'),
  model: () => resolveModel('reviewer') as never,
});

const IntentSchema = z.object({
  intent: z.enum(['DELETION', 'NON_DELETION']),
  request_type: z.string(),
  confidence: z.number(),
  rationale: z.string(),
});

/**
 * Intent: rules always run; the LLM agent runs when configured. Explicit "do not delete" statements found by the
 * rules are never overridden, otherwise the LLM decides and disagreements are recorded in the trace.
 */
export async function classifyIntent(text: string): Promise<IntentResult> {
  const rules = classifyIntentRules(text);
  if (!llmEnabled('intent')) return rules;
  const model = modelFor('intent');
  try {
    const res = await intentAgent.generate(`Customer message:\n"""\n${text}\n"""`, {
      structuredOutput: { schema: IntentSchema },
      modelSettings: { temperature: 0 },
    });
    usageTracker.record('intent', model, res.totalUsage);
    const llm = res.object as z.infer<typeof IntentSchema>;
    if (rules.intent === 'NON_DELETION' && rules.confidence >= 0.95 && llm.intent === 'DELETION') {
      return { ...rules, source: 'llm+rules', rationale: `${rules.rationale}; LLM disagreed (${llm.rationale}) - explicit non-deletion wins` };
    }
    return {
      intent: llm.intent,
      confidence: llm.confidence,
      requestType: llm.request_type,
      rationale: llm.intent === rules.intent ? llm.rationale : `${llm.rationale} (rules said ${rules.intent})`,
      source: 'llm+rules',
    };
  } catch (err) {
    usageTracker.record('intent', model, undefined, true);
    return { ...rules, rationale: `${rules.rationale}; LLM unavailable: ${(err as Error).message.slice(0, 80)}` };
  }
}

const VisionSchema = z.object({
  records: z.array(
    z.object({
      resource_id: z.string(),
      system: z.string(),
      state: z.enum(['PRESENT', 'ABSENT', 'IDENTIFIABLE', 'ANONYMIZED', 'REDACTED', 'ACTIVE', 'DISABLED', 'RESTORABLE', 'BLOCKED']),
      category: z.string(),
      identifiers: z.array(z.string()),
      is_subject: z.boolean(),
    }),
  ),
});

function toToken(v: string): string | null {
  const s = v.trim();
  if (/^user_\d+$/.test(s)) return `user:${s}`;
  if (/@/.test(s)) return `email:${normEmail(s)}`;
  if (/^\+?[\d\s().-]{7,}$/.test(s)) return `phone:${normPhone(s)}`;
  return null;
}

/** Mastra-agent screenshot/PDF extraction. Returns null (OCR rules fallback) when no vision model is configured. */
export async function extractMediaWithAgent(input: { fileId: string; filePath: string; origin: string; capturedAt: string; text: string }): Promise<ParsedEvidence | null> {
  if (!llmEnabled('vision')) return null;
  const model = modelFor('vision');
  const isPdf = input.filePath.toLowerCase().endsWith('.pdf');
  try {
    const content: unknown[] = [
      { type: 'text', text: `Artifact origin: ${input.origin}; captured_at: ${input.capturedAt}.\nOCR/PDF text (may contain OCR errors):\n${input.text.slice(0, 6000)}` },
    ];
    if (!isPdf) content.push({ type: 'image', image: fs.readFileSync(input.filePath), mimeType: 'image/png' });
    const res = await evidenceVisionAgent.generate([{ role: 'user', content }] as never, {
      structuredOutput: { schema: VisionSchema },
      modelSettings: { temperature: 0 },
    });
    usageTracker.record('vision', model, res.totalUsage);
    const parsed = res.object as z.infer<typeof VisionSchema>;
    const observations: Observation[] = parsed.records.map((r) => ({
      evidenceId: input.fileId,
      kind: 'file',
      capturedAt: input.capturedAt,
      observedAt: input.capturedAt,
      system: systemFromResource(r.resource_id) ?? r.system,
      resourceId: r.resource_id,
      category: r.is_subject ? r.category : undefined,
      state: r.is_subject ? (r.state as ObservedState) : undefined,
      identifiers: r.is_subject ? r.identifiers.map(toToken).filter((t): t is string => !!t) : [],
      parser: 'llm-vision',
      note: r.is_subject ? 'screen-subject' : 'screen-reference',
    }));
    return { observations, scopes: [], text: input.text };
  } catch {
    usageTracker.record('vision', model, undefined, true);
    return null;
  }
}

const ReviewSchema = z.object({
  concerns: z.array(z.object({ resource_id: z.string(), problem: z.string(), evidence_id: z.string() })),
  confidence: z.number(),
});

/** Optional reviewer (AUDIT_LLM_REVIEW=1). Its notes go to the trace; it never changes the submitted steps. */
export async function reviewAudit(summary: unknown): Promise<z.infer<typeof ReviewSchema> | null> {
  if (!llmEnabled('reviewer')) return null;
  const model = modelFor('reviewer');
  try {
    const res = await auditReviewerAgent.generate(JSON.stringify(summary), { structuredOutput: { schema: ReviewSchema }, modelSettings: { temperature: 0 } });
    usageTracker.record('reviewer', model, res.totalUsage);
    return res.object as z.infer<typeof ReviewSchema>;
  } catch {
    usageTracker.record('reviewer', model, undefined, true);
    return null;
  }
}

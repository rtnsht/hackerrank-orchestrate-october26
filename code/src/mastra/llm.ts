// LLM configuration and token/cost accounting for the Mastra agents.
//
// The audit is evidence-first and deterministic; LLM agents are used where language or vision understanding adds
// value (request intent, screenshot/PDF interpretation, optional reviewer). Every agent call is metered here so the
// final run's usage can be written to evaluation/usage_report.md.

export type AgentRole = 'intent' | 'vision' | 'reviewer';

const PROVIDER_KEYS: Record<string, string[]> = {
  anthropic: ['ANTHROPIC_API_KEY'],
  openai: ['OPENAI_API_KEY'],
  google: ['GOOGLE_GENERATIVE_AI_API_KEY', 'GEMINI_API_KEY'],
  groq: ['GROQ_API_KEY'],
  mistral: ['MISTRAL_API_KEY'],
  openrouter: ['OPENROUTER_API_KEY'],
};

/** USD per 1M tokens (input, output). Used only for the cost estimate in the usage report. */
export const PRICING: Record<string, { input: number; output: number }> = {
  'anthropic/claude-haiku-5-5': { input: 1, output: 5 },
  'anthropic/claude-sonnet-5-5': { input: 3, output: 15 },
  'anthropic/claude-opus-5-5': { input: 5, output: 25 },
  'anthropic/claude-haiku-4-5': { input: 1, output: 5 },
  'anthropic/claude-sonnet-4-5': { input: 3, output: 15 },
  'openai/gpt-5-mini': { input: 0.25, output: 2 },
  'openai/gpt-5': { input: 1.25, output: 10 },
};

export const DEFAULT_MODELS: Record<AgentRole, string> = {
  intent: 'anthropic/claude-haiku-5-5',
  vision: 'anthropic/claude-sonnet-5-5',
  reviewer: 'anthropic/claude-sonnet-5-5',
};

export function modelFor(role: AgentRole): string {
  if (overrides[role]) return `mock/${role}-mock-model`;
  const env = { intent: 'AUDIT_INTENT_MODEL', vision: 'AUDIT_VISION_MODEL', reviewer: 'AUDIT_REVIEW_MODEL' }[role];
  return process.env[env] ?? process.env.AUDIT_MODEL ?? DEFAULT_MODELS[role];
}

/** Test hook: inject a language-model object (e.g. Mastra's mock model) for one agent role. */
const overrides: Partial<Record<AgentRole, unknown>> = {};
export function setModelOverride(role: AgentRole, model: unknown): void {
  overrides[role] = model;
}

/** What the Mastra Agent actually receives: an injected model object, or a "provider/model" router id. */
export function resolveModel(role: AgentRole): unknown {
  return overrides[role] ?? modelFor(role);
}

/** LLM agents run only when explicitly allowed (AUDIT_LLM != off) and the provider's key is in the environment. */
export function llmEnabled(role: AgentRole): boolean {
  if (overrides[role]) return true;
  const mode = (process.env.AUDIT_LLM ?? 'auto').toLowerCase();
  if (mode === 'off' || mode === '0' || mode === 'false') return false;
  if (role === 'reviewer' && !['1', 'true', 'on'].includes((process.env.AUDIT_LLM_REVIEW ?? '').toLowerCase())) return false;
  if (role === 'vision' && ['0', 'false', 'off'].includes((process.env.AUDIT_LLM_VISION ?? '1').toLowerCase())) return false;
  const provider = modelFor(role).split('/')[0];
  return (PROVIDER_KEYS[provider] ?? []).some((k) => !!process.env[k]);
}

export interface UsageRecord {
  role: AgentRole;
  model: string;
  calls: number;
  failures: number;
  inputTokens: number;
  outputTokens: number;
}

class UsageTracker {
  private rows = new Map<string, UsageRecord>();
  record(role: AgentRole, model: string, usage: { inputTokens?: number; outputTokens?: number } | undefined, failed = false) {
    const key = `${role}|${model}`;
    const r = this.rows.get(key) ?? { role, model, calls: 0, failures: 0, inputTokens: 0, outputTokens: 0 };
    r.calls++;
    if (failed) r.failures++;
    r.inputTokens += usage?.inputTokens ?? 0;
    r.outputTokens += usage?.outputTokens ?? 0;
    this.rows.set(key, r);
  }
  snapshot(): UsageRecord[] {
    return [...this.rows.values()].sort((a, b) => a.role.localeCompare(b.role) || a.model.localeCompare(b.model));
  }
}

export const usageTracker = new UsageTracker();

export function estimateCost(model: string, inputTokens: number, outputTokens: number): number | null {
  const p = PRICING[model];
  if (!p) return null;
  return (inputTokens * p.input + outputTokens * p.output) / 1_000_000;
}

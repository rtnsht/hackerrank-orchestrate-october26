// LLM configuration and token/cost accounting for the Mastra agents.
//
// The audit is evidence-first and deterministic; LLM agents are used where language or vision understanding adds
// value (request intent, screenshot/PDF interpretation, optional reviewer). Every agent is configured independently
// through AUDIT_<AGENT>_* environment variables (see code/.env.example), so each can use a different provider, model,
// key, endpoint and temperature. Every agent call is metered here so the final run's usage can be written to
// evaluation/usage_report.md.

export type AgentRole = 'intent' | 'vision' | 'reviewer';

/** Environment-variable prefix for each agent. */
export const ROLE_PREFIX: Record<AgentRole, string> = {
  intent: 'AUDIT_INTENT',
  vision: 'AUDIT_VISION',
  reviewer: 'AUDIT_REVIEW',
};

/** Provider-wide keys read by Mastra's model router, by provider prefix of the model id. */
export const PROVIDER_KEYS: Record<string, string[]> = {
  anthropic: ['ANTHROPIC_API_KEY'],
  openai: ['OPENAI_API_KEY'],
  google: ['GOOGLE_GENERATIVE_AI_API_KEY', 'GEMINI_API_KEY'],
  groq: ['GROQ_API_KEY'],
  mistral: ['MISTRAL_API_KEY'],
  openrouter: ['OPENROUTER_API_KEY'],
  xai: ['XAI_API_KEY'],
  deepseek: ['DEEPSEEK_API_KEY'],
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

/** Default on/off mode per agent: intent and vision run when a key is available, the reviewer is opt-in. */
const DEFAULT_ENABLED: Record<AgentRole, string> = { intent: 'auto', vision: 'auto', reviewer: 'false' };

/** Older variable names kept working (AUDIT_LLM_VISION=0, AUDIT_LLM_REVIEW=1). */
const LEGACY_ENABLED: Record<AgentRole, string | undefined> = { intent: undefined, vision: 'AUDIT_LLM_VISION', reviewer: 'AUDIT_LLM_REVIEW' };

const env = (name: string): string | undefined => {
  const v = process.env[name];
  return v === undefined || v.trim() === '' ? undefined : v.trim();
};
const isOn = (v: string) => ['1', 'true', 'on', 'yes'].includes(v.toLowerCase());
const isOff = (v: string) => ['0', 'false', 'off', 'no'].includes(v.toLowerCase());

export interface AgentSettings {
  role: AgentRole;
  /** "auto" (run when credentials exist), "true" or "false" */
  enabled: string;
  /** Mastra model-router id, "provider/model" */
  model: string;
  /** per-agent key; falls back to the provider-wide key (e.g. ANTHROPIC_API_KEY) */
  apiKey?: string;
  /** per-agent endpoint for OpenAI-compatible / self-hosted / proxy deployments */
  baseUrl?: string;
  temperature: number;
  maxOutputTokens?: number;
  /** optional per-agent price override, USD per 1M tokens */
  price?: { input: number; output: number };
}

/** Resolve one agent's settings from AUDIT_<AGENT>_* variables, then shared AUDIT_* defaults, then built-ins. */
export function agentSettings(role: AgentRole): AgentSettings {
  const p = ROLE_PREFIX[role];
  const legacy = LEGACY_ENABLED[role];
  const enabled = env(`${p}_ENABLED`) ?? (legacy ? env(legacy) : undefined) ?? DEFAULT_ENABLED[role];
  const temperature = Number(env(`${p}_TEMPERATURE`) ?? env('AUDIT_TEMPERATURE') ?? 0);
  const maxOut = env(`${p}_MAX_OUTPUT_TOKENS`) ?? env('AUDIT_MAX_OUTPUT_TOKENS');
  const pin = env(`${p}_PRICE_INPUT_PER_MTOK`);
  const pout = env(`${p}_PRICE_OUTPUT_PER_MTOK`);
  return {
    role,
    enabled,
    model: env(`${p}_MODEL`) ?? env('AUDIT_MODEL') ?? DEFAULT_MODELS[role],
    apiKey: env(`${p}_API_KEY`),
    baseUrl: env(`${p}_BASE_URL`) ?? env('AUDIT_BASE_URL'),
    temperature: Number.isFinite(temperature) ? temperature : 0,
    maxOutputTokens: maxOut ? Number(maxOut) : undefined,
    price: pin && pout ? { input: Number(pin), output: Number(pout) } : undefined,
  };
}

/** Test hook: inject a language-model object (e.g. Mastra's mock model) for one agent role. */
const overrides: Partial<Record<AgentRole, unknown>> = {};
export function setModelOverride(role: AgentRole, model: unknown): void {
  overrides[role] = model;
}

export function modelFor(role: AgentRole): string {
  if (overrides[role]) return `mock/${role}-mock-model`;
  return agentSettings(role).model;
}

/**
 * What the Mastra Agent receives: an injected model object, a router config with this agent's own key / endpoint,
 * or a plain "provider/model" router id (provider-wide key from the environment).
 */
export function resolveModel(role: AgentRole): unknown {
  if (overrides[role]) return overrides[role];
  const s = agentSettings(role);
  if (s.apiKey || s.baseUrl) return { id: s.model, ...(s.apiKey ? { apiKey: s.apiKey } : {}), ...(s.baseUrl ? { url: s.baseUrl } : {}) };
  return s.model;
}

/** Whether credentials exist for an agent: its own key, a custom endpoint, or the provider-wide key. */
export function hasCredentials(role: AgentRole): boolean {
  const s = agentSettings(role);
  if (s.apiKey || s.baseUrl) return true;
  const provider = s.model.split('/')[0];
  return (PROVIDER_KEYS[provider] ?? []).some((k) => !!env(k));
}

/**
 * An agent runs when the global switch allows it (AUDIT_LLM != off), its own switch is "auto" or "true", and it has
 * credentials. Without credentials it always falls back to its deterministic counterpart.
 */
export function llmEnabled(role: AgentRole): boolean {
  if (overrides[role]) return true;
  const global = env('AUDIT_LLM') ?? 'auto';
  if (isOff(global)) return false;
  const s = agentSettings(role);
  if (isOff(s.enabled)) return false;
  if (!isOn(s.enabled) && s.enabled.toLowerCase() !== 'auto') return false;
  return hasCredentials(role);
}

/** Generation options shared by every agent call (structured output is added by the caller). */
export function modelSettings(role: AgentRole): { temperature: number; maxOutputTokens?: number } {
  const s = agentSettings(role);
  return { temperature: s.temperature, ...(s.maxOutputTokens ? { maxOutputTokens: s.maxOutputTokens } : {}) };
}

/** One-line description of every agent's configuration (secrets never printed). */
export function describeAgentConfig(): string[] {
  return (Object.keys(ROLE_PREFIX) as AgentRole[]).map((r) => {
    const s = agentSettings(r);
    const key = s.apiKey ? 'own key' : hasCredentials(r) ? 'provider key' : 'no key';
    return `${r}: ${llmEnabled(r) ? 'ON' : 'off'} (enabled=${s.enabled}, model=${s.model}, ${key}${s.baseUrl ? ', custom endpoint' : ''}, temperature=${s.temperature})`;
  });
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

/** Estimated cost: the agent's own price override first, then the built-in price table. */
export function estimateCost(model: string, inputTokens: number, outputTokens: number, role?: AgentRole): number | null {
  const p = (role && agentSettings(role).price) || PRICING[model];
  if (!p) return null;
  return (inputTokens * p.input + outputTokens * p.output) / 1_000_000;
}

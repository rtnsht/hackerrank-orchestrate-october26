// LLM wiring smoke test (no API key needed): injects Mastra's mock language model into the intent and vision
// agents and drives the *production* code paths (classifyIntent, extractMediaWithAgent) end to end, including
// structured-output parsing, the rule cross-check and token metering. It proves integration, not model quality.
import path from 'node:path';
import { createMockModel } from '@mastra/core/test-utils/llm-mock';
import { CODE_ROOT, REPO_ROOT } from '../core/dataset.js';
import { classifyIntent, extractMediaWithAgent } from '../mastra/agents/index.js';
import { setModelOverride, usageTracker } from '../mastra/llm.js';

setModelOverride('intent', createMockModel({ version: 'v2', mockText: { intent: 'DELETION', request_type: 'deletion', confidence: 0.9, rationale: 'asks to delete the account' } }));
setModelOverride('vision', createMockModel({ version: 'v2', mockText: { records: [{ resource_id: 'data_flow_1121', system: 'integration_service', state: 'ACTIVE', category: 'DATA_FLOW', identifiers: ['jain.aditi@gmail.com'], is_subject: true }] } }));

const texts = [
  'Please permanently delete the account associated with this email address.',
  'Please remove me from marketing lists only. I still want receipts and service messages for this account.',
];
for (const t of texts) {
  const r = await classifyIntent(t);
  console.log(`intent  | source=${r.source} intent=${r.intent} | ${r.rationale.slice(0, 110)}`);
}
const shot = path.join(REPO_ROOT, 'dataset', 'evidence', 'images', 'connector_management_console_20260807.png');
const media = await extractMediaWithAgent({ fileId: 'evidence_14', filePath: shot, origin: 'integration_admin', capturedAt: '2026-08-07T08:00:00Z', text: 'Connections data_flow_1121' });
console.log('vision  |', JSON.stringify(media?.observations.map((o) => ({ resource: o.resourceId, state: o.state, ids: o.identifiers, parser: o.parser }))));
console.log('usage   |', JSON.stringify(usageTracker.snapshot()));
void CODE_ROOT;

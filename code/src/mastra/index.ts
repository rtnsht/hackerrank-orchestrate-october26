// Mastra instance: registers the agents and the deletion-audit workflow (also usable from `mastra dev` / Studio).
import { Mastra } from '@mastra/core';
import { auditReviewerAgent, evidenceVisionAgent, intentAgent } from './agents/index.js';
import { deletionAuditWorkflow } from './workflows/deletion-audit-workflow.js';

export const mastra = new Mastra({
  agents: { intentAgent, evidenceVisionAgent, auditReviewerAgent },
  workflows: { deletionAuditWorkflow },
  logger: false,
});

// Deterministic multilingual request-intent classifier (used directly, and as the fallback / cross-check for the
// Mastra LLM intent agent).

export type Intent = 'DELETION' | 'NON_DELETION';

export interface IntentResult {
  intent: Intent;
  confidence: number;
  requestType: string; // deletion | access | export | correction | marketing_opt_out | account_support | billing | other
  rationale: string;
  source: 'rules' | 'llm' | 'llm+rules';
}

const DELETION_PATTERNS: RegExp[] = [
  /\b(delet(e|ed|es|ing|ion)|eras(e|ed|ing|ure)|right to be forgotten|wipe|purge)\b/i,
  /\b(remov(e|ed|ing)|clos(e|ed|ing)|get rid of)\b[^.?!]{0,60}\b(account|profile|personal (data|details|information)|records?|search result|copy|copies|traces|data)\b/i,
  /\b(account|profile)\b[^.?!]{0,80}\b(closed|removed|gone|deleted)\b/i,
  /\b(eliminar|eliminación|elimine|borrar|supresión)\b/i, // es
  /\b(exclusão|excluir|apagar|eliminação)\b/i, // pt
  /\b(suppression|supprimer|effacer|effacement)\b/i, // fr
  /\b(löschen|löschung|entfernen)\b/i, // de
  /삭제|탈퇴/, // ko
  /削除|退会|消去/, // ja
  /删除|注销/, // zh
];

// Explicit statements that the person does NOT want deletion.
const NEGATION_PATTERNS: RegExp[] = [
  /\bnot (asking|requesting) (for )?(deletion|to (delete|erase|close))/i,
  /\b(do not|don't|dont|please don't|please do not) (delete|erase|close|remove|change)\b/i,
  /\bnot a request to (erase|delete)/i,
  /\bwithout (removing|deleting)\b/i,
  /\b(leave|keep) (the|my) (account|profile)\b/i,
  /\bkeep the account as it is\b/i,
  /\bleave my (profile|account)( and [\w ]+)? in place\b/i,
  /\bcontinue using the service\b/i,
  /\bI still (use|need|want) (it|the account|my account|receipts)/i,
  /\bmarketing lists only\b/i,
];

const NON_DELETION_TYPES: [string, RegExp][] = [
  ['access', /\b(what personal information|copy of the information|information (you )?hold|access request|send me a copy)\b/i],
  ['export', /\b(download my (account )?information|portable format|data portability|export my data)\b/i],
  ['correction', /\b(misspelled|correct it|out of date|replace it with the new|update my|change the email address|fix my)\b/i],
  ['marketing_opt_out', /\b(stop the promotional emails|marketing lists|unsubscribe|opt out)\b/i],
  ['account_support', /\b(can't access|cannot access|resetting the login|reset (my )?password|locked out)\b/i],
  ['billing', /\b(invoices?|receipt|billing)\b/i],
];

export function classifyIntentRules(text: string): IntentResult {
  const t = text.normalize('NFC');
  const negated = NEGATION_PATTERNS.find((p) => p.test(t));
  const del = DELETION_PATTERNS.find((p) => p.test(t));
  const nonDel = NON_DELETION_TYPES.find(([, p]) => p.test(t));
  if (negated) {
    return {
      intent: 'NON_DELETION',
      confidence: 0.95,
      requestType: nonDel?.[0] ?? 'other',
      rationale: `explicit non-deletion statement (${negated.source.slice(0, 40)}...)`,
      source: 'rules',
    };
  }
  if (del) {
    return { intent: 'DELETION', confidence: nonDel ? 0.7 : 0.95, requestType: 'deletion', rationale: `deletion request phrase matched (${del.source.slice(0, 40)}...)`, source: 'rules' };
  }
  return {
    intent: 'NON_DELETION',
    confidence: nonDel ? 0.9 : 0.6,
    requestType: nonDel?.[0] ?? 'other',
    rationale: nonDel ? `non-deletion request type: ${nonDel[0]}` : 'no deletion authorization found',
    source: 'rules',
  };
}

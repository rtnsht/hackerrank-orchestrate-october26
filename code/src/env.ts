// Loads agent configuration from code/.env (or the file named by AUDIT_ENV_FILE) before anything else runs.
// Uses Node's built-in loader: variables already set in the shell always win over the file. The file is git-ignored;
// copy code/.env.example to code/.env to start.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const codeRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const candidate = process.env.AUDIT_ENV_FILE ? path.resolve(process.env.AUDIT_ENV_FILE) : path.join(codeRoot, '.env');

export const loadedEnvFile: string | null = fs.existsSync(candidate) ? candidate : null;
if (loadedEnvFile) process.loadEnvFile(loadedEnvFile);

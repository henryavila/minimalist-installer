import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { join } from 'node:path';

// Default manifest directory. Package-neutral — a consumer (e.g. atomic-skills)
// overrides it via the `manifestDir` argument to point at its own location
// (atomic-skills uses `.atomic-skills`). The engine itself never hardcodes a
// consumer-specific name.
export const MANIFEST_DIR = '.tooling-installer';
export const MANIFEST_FILE = 'manifest.json';

export function readManifest(projectDir, manifestDir = MANIFEST_DIR) {
  const filePath = join(projectDir, manifestDir, MANIFEST_FILE);
  if (!existsSync(filePath)) return null;
  const raw = readFileSync(filePath, 'utf8');
  return JSON.parse(raw);
}

export function writeManifest(projectDir, data, manifestDir = MANIFEST_DIR) {
  const dir = join(projectDir, manifestDir);
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
  const filePath = join(dir, MANIFEST_FILE);
  data.updated_at = new Date().toISOString();
  if (!data.installed_at) data.installed_at = data.updated_at;
  writeFileSync(filePath, JSON.stringify(data, null, 2) + '\n', 'utf8');
}

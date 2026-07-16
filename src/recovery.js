/**
 * Conservative incomplete-transaction recovery.
 *
 * First recovery release: fail closed on incomplete transactions and expose a
 * deterministic inspect path. No automatic destructive recovery.
 */
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { readManifest, MANIFEST_DIR, MANIFEST_FILE } from './manifest.js';

export const TX_STATE_COMPLETE = 'complete';
export const TX_STATE_INCOMPLETE = 'incomplete';
export const TX_STATE_ABSENT = 'absent';

/**
 * @returns {{ state: string, manifest: object|null, reason?: string }}
 */
export function inspectTransaction(projectDir, manifestDir = MANIFEST_DIR) {
  const filePath = join(projectDir, manifestDir, MANIFEST_FILE);
  if (!existsSync(filePath)) {
    return { state: TX_STATE_ABSENT, manifest: null };
  }

  let raw;
  try {
    raw = readFileSync(filePath, 'utf8');
  } catch (err) {
    return { state: TX_STATE_INCOMPLETE, manifest: null, reason: `unreadable: ${err.message}` };
  }

  let manifest;
  try {
    manifest = JSON.parse(raw);
  } catch (err) {
    return { state: TX_STATE_INCOMPLETE, manifest: null, reason: `invalid json: ${err.message}` };
  }

  const txState = manifest.transaction?.state ?? TX_STATE_COMPLETE;
  if (txState !== TX_STATE_COMPLETE) {
    return {
      state: TX_STATE_INCOMPLETE,
      manifest,
      reason: `transaction.state=${txState}`,
    };
  }

  return { state: TX_STATE_COMPLETE, manifest };
}

/**
 * Fail closed if an incomplete transaction is present. Callers must not mutate.
 */
export function assertNoIncompleteTransaction(projectDir, manifestDir = MANIFEST_DIR) {
  const info = inspectTransaction(projectDir, manifestDir);
  if (info.state === TX_STATE_INCOMPLETE) {
    const err = new Error(
      `Incomplete installer transaction at ${projectDir}: ${info.reason ?? 'unknown'}. `
      + 'Inspect with inspectTransaction(); automatic recovery is disabled.',
    );
    err.code = 'INCOMPLETE_TRANSACTION';
    err.inspect = info;
    throw err;
  }
  return info;
}

/**
 * Read-only convenience for CLI/debug (no mutation).
 */
export function describeRecovery(projectDir, manifestDir = MANIFEST_DIR) {
  const info = inspectTransaction(projectDir, manifestDir);
  const effects = info.manifest?.effects ?? [];
  return {
    ...info,
    effectCount: effects.length,
    journalVersion: info.manifest?.journalVersion ?? (info.manifest ? 1 : null),
  };
}

// Re-export read for consumers that want manifest + recovery together.
export { readManifest };

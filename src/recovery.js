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
 *
 * Post-F-001 journals set `transaction.journalMode === 'per-effect'`: an
 * incomplete journal's `effects` list is trusted to match applied ownership
 * (durable flush after each apply). Pre-U incomplete journals may still hold
 * only prior effects while disk already diverged — consumers must not silent-
 * resume those (see atomic-skills P0-A trust classification).
 */
export function describeRecovery(projectDir, manifestDir = MANIFEST_DIR) {
  const info = inspectTransaction(projectDir, manifestDir);
  const effects = info.manifest?.effects ?? [];
  const journalMode = info.manifest?.transaction?.journalMode ?? null;
  const appliedCount = info.manifest?.transaction?.appliedCount;
  return {
    ...info,
    effectCount: effects.length,
    journalVersion: info.manifest?.journalVersion ?? (info.manifest ? 1 : null),
    journalMode,
    appliedCount: typeof appliedCount === 'number' ? appliedCount : effects.length,
    /** True when incomplete journal is trustworthy for reverse/resume of listed effects. */
    durablePerEffect: journalMode === 'per-effect',
    effectIds: effects.map((e) => e.id ?? e.type),
  };
}

/**
 * Journaled effects currently on disk (complete or incomplete). Read-only.
 * Useful for force-incomplete reverse and for consumers building resume plans.
 * Does not mutate and does not clear incomplete state.
 */
export function readJournaledEffects(projectDir, manifestDir = MANIFEST_DIR) {
  const desc = describeRecovery(projectDir, manifestDir);
  return {
    state: desc.state,
    reason: desc.reason,
    durablePerEffect: desc.durablePerEffect,
    journalMode: desc.journalMode,
    effects: desc.manifest?.effects ?? [],
  };
}

// Re-export read for consumers that want manifest + recovery together.
export { readManifest };

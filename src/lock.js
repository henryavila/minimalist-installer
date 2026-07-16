/**
 * Canonical multi-resource lock coordinator.
 *
 * Identity: `v1\0<kind>\0<canonicalTarget>` (raw bytes, not hashed for sort order).
 * Lock files: SHA-256(identity) under a single user-scoped lockRoot.
 * Acquisition: declare full set → sort by identity bytes → dedupe → acquire in order.
 * Release: reverse order. No late acquisition after the first mutation.
 */
import {
  openSync,
  closeSync,
  writeSync,
  readFileSync,
  unlinkSync,
  mkdirSync,
  existsSync,
  constants,
} from 'node:fs';
import { createHash } from 'node:crypto';
import { join, resolve } from 'node:path';
import { homedir } from 'node:os';

export const DEFAULT_LOCK_ROOT = join(homedir(), '.minimalist-installer', 'locks');

/**
 * @param {string} kind
 * @param {string} canonicalTarget absolute canonical path (or opaque target)
 */
export function resourceIdentity(kind, canonicalTarget) {
  if (typeof kind !== 'string' || kind.length === 0) {
    throw new Error('resource kind must be a non-empty string');
  }
  if (typeof canonicalTarget !== 'string' || canonicalTarget.length === 0) {
    throw new Error('canonicalTarget must be a non-empty string');
  }
  return `v1\0${kind}\0${canonicalTarget}`;
}

export function lockFileName(identity) {
  return `${createHash('sha256').update(identity, 'utf8').digest('hex')}.lock`;
}

function isPidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

/**
 * Acquire exclusive locks for the given resource identities.
 * @param {string[]} identities
 * @param {{ lockRoot?: string, timeoutMs?: number, pollMs?: number }} [opts]
 * @returns {{ identities: string[], lockRoot: string, release: () => void }}
 */
export function acquireLocks(identities, opts = {}) {
  const lockRoot = resolve(opts.lockRoot ?? DEFAULT_LOCK_ROOT);
  const timeoutMs = opts.timeoutMs ?? 30_000;
  const pollMs = opts.pollMs ?? 25;

  const unique = [...new Set(identities.filter(Boolean))];
  unique.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));

  mkdirSync(lockRoot, { recursive: true });

  /** @type {string[]} */
  const held = [];
  const started = Date.now();

  const release = () => {
    for (const id of [...held].reverse()) {
      const file = join(lockRoot, lockFileName(id));
      try {
        const raw = readFileSync(file, 'utf8');
        const meta = JSON.parse(raw);
        if (meta.pid === process.pid) unlinkSync(file);
      } catch {
        try { unlinkSync(file); } catch { /* ignore */ }
      }
    }
    held.length = 0;
  };

  try {
    for (const identity of unique) {
      const file = join(lockRoot, lockFileName(identity));
      for (;;) {
        try {
          const fd = openSync(file, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o644);
          try {
            writeSync(fd, JSON.stringify({
              pid: process.pid,
              identity,
              acquiredAt: new Date().toISOString(),
            }, null, 2) + '\n');
          } finally {
            closeSync(fd);
          }
          held.push(identity);
          break;
        } catch (err) {
          if (err.code !== 'EEXIST') throw err;
          // Stale lock recovery: dead PID → steal. Never unlink on parse failure
          // alone — a concurrent holder may still be writing the lock payload.
          try {
            const raw = readFileSync(file, 'utf8');
            if (raw.trim().length > 0) {
              const meta = JSON.parse(raw);
              if (Number.isInteger(meta.pid) && !isPidAlive(meta.pid)) {
                unlinkSync(file);
                continue;
              }
            }
          } catch {
            // Unreadable/partial lock: wait; do not steal.
          }
          if (Date.now() - started > timeoutMs) {
            release();
            const e = new Error(`Timeout acquiring lock for ${identity}`);
            e.code = 'LOCK_TIMEOUT';
            throw e;
          }
          // Yield the CPU while waiting (tight spin starves sibling processes).
          const waitBuf = new Int32Array(new SharedArrayBuffer(4));
          Atomics.wait(waitBuf, 0, 0, pollMs);
        }
      }
    }
  } catch (err) {
    release();
    throw err;
  }

  return { identities: unique, lockRoot, release };
}

/**
 * Helper: declare install-root + optional extras, acquire in total order.
 */
export function acquireInstallLocks({ projectDir, extra = [], lockRoot } = {}) {
  const ids = [
    resourceIdentity('install-root', resolve(projectDir)),
    ...extra,
  ];
  return acquireLocks(ids, { lockRoot });
}

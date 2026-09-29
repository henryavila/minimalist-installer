import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { acquireLocks, resourceIdentity, lockFileName } from '../src/lock.js';
import { createHash } from 'node:crypto';

describe('lock order', () => {
  let root;
  afterEach(() => {
    if (root) rmSync(root, { recursive: true, force: true });
    root = undefined;
  });

  it('dedupes and sorts identities by raw identity bytes before acquire', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-lock-order-'));
    const lockRoot = join(root, 'locks');
    const z = resourceIdentity('z-kind', '/z');
    const a = resourceIdentity('a-kind', '/a');
    const locks = acquireLocks([z, a, z], { lockRoot });
    assert.deepEqual(locks.identities, [a, z].sort((x, y) => (x < y ? -1 : x > y ? 1 : 0)));
    locks.release();
  });

  it('names lock files by sha256 of identity', () => {
    const id = resourceIdentity('install-root', '/tmp/x');
    const expected = `${createHash('sha256').update(id, 'utf8').digest('hex')}.lock`;
    assert.equal(lockFileName(id), expected);
  });
});

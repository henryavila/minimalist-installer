import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import { mkdtempSync, writeFileSync, readFileSync, rmSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { acquireLocks, resourceIdentity } from '../src/lock.js';

describe('concurrency / shared locks', () => {
  let root;
  afterEach(() => {
    if (root) rmSync(root, { recursive: true, force: true });
    root = undefined;
  });

  it('serializes overlapping resources acquired in opposite order', async () => {
    root = mkdtempSync(join(tmpdir(), 'mi-conc-'));
    const lockRoot = join(root, 'locks');
    const registry = join(root, 'registry.json');
    writeFileSync(registry, '[]\n', 'utf8');

    const idA = resourceIdentity('registry', registry);
    const idB = resourceIdentity('install-root', join(root, 'a'));
    const idC = resourceIdentity('install-root', join(root, 'b'));

    const worker = (order, value) => `
import { readFileSync, writeFileSync } from 'node:fs';
import { acquireLocks } from ${JSON.stringify(new URL('../src/lock.js', import.meta.url).href)};
const identities = ${JSON.stringify(order)};
const locks = acquireLocks(identities, { lockRoot: ${JSON.stringify(lockRoot)}, timeoutMs: 10000 });
try {
  let list = JSON.parse(readFileSync(${JSON.stringify(registry)}, 'utf8'));
  await new Promise(r => setTimeout(r, 40));
  list.push(${JSON.stringify(value)});
  writeFileSync(${JSON.stringify(registry)}, JSON.stringify(list) + '\\n');
} finally {
  locks.release();
}
`;

    const run = (order, value) => new Promise((resolve, reject) => {
      const child = spawn(process.execPath, ['--input-type=module', '-e', worker(order, value)], {
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      let err = '';
      child.stderr.on('data', (d) => { err += d; });
      child.on('exit', (code) => {
        if (code === 0) resolve();
        else reject(new Error(`worker exit ${code}: ${err}`));
      });
    });

    // Opposite acquisition order for overlapping {registry, root} sets.
    await Promise.all([
      run([idA, idB], 'A'),
      run([idC, idA], 'B'),
    ]);

    const list = JSON.parse(readFileSync(registry, 'utf8'));
    assert.deepEqual(list.sort(), ['A', 'B']);
  });

  it('allows disjoint resources to progress without waiting on each other', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-disjoint-'));
    const lockRoot = join(root, 'locks');
    mkdirSync(lockRoot, { recursive: true });
    const a = acquireLocks([resourceIdentity('install-root', join(root, 'a'))], { lockRoot });
    const b = acquireLocks([resourceIdentity('install-root', join(root, 'b'))], { lockRoot });
    assert.equal(a.identities.length, 1);
    assert.equal(b.identities.length, 1);
    a.release();
    b.release();
  });
});

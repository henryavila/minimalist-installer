import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { defineInstaller, createFileSetProvider } from '../src/index.js';
import { hashContent } from '../src/hash.js';

describe('update retry', () => {
  let root;
  afterEach(() => {
    if (root) rmSync(root, { recursive: true, force: true });
    root = undefined;
  });

  it('retry after content already matches desired records new hash (already-desired)', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-retry-'));
    const lockRoot = join(root, 'locks');
    const v1 = defineInstaller({
      providers: [createFileSetProvider()],
      config: {
        manifestDir: '.mi',
        lockRoot,
        files: [{ path: 'a.txt', content: 'V1' }],
      },
    });
    v1.install({ projectDir: root });

    // Simulate partial update: disk already V2 but journal still V1.
    writeFileSync(join(root, 'a.txt'), 'V2', 'utf8');
    const v2 = defineInstaller({
      providers: [createFileSetProvider()],
      config: {
        manifestDir: '.mi',
        lockRoot,
        files: [{ path: 'a.txt', content: 'V2' }],
      },
    });
    const m = v2.install({ projectDir: root });
    const entry = m.effects.find((e) => e.type === 'reconcileFileSet');
    const tracked = entry.beforeState.find((f) => f.path === 'a.txt');
    assert.equal(tracked.installedHash, hashContent('V2'));
    assert.equal(readFileSync(join(root, 'a.txt'), 'utf8'), 'V2');

    v2.uninstall({ projectDir: root });
    // V2 was tracked → removed on uninstall (not left as false user edit).
    assert.equal(
      (() => { try { return readFileSync(join(root, 'a.txt'), 'utf8'); } catch { return null; } })(),
      null,
    );
  });
});

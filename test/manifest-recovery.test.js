import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { writeManifest, readManifest, MANIFEST_DIR } from '../src/manifest.js';
import { inspectTransaction, assertNoIncompleteTransaction } from '../src/recovery.js';
import { defineInstaller, createFileSetProvider } from '../src/index.js';

describe('manifest recovery', () => {
  let root;
  afterEach(() => {
    if (root) rmSync(root, { recursive: true, force: true });
    root = undefined;
  });

  it('writeManifest is atomic (temp→rename) and round-trips JSON', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-manifest-'));
    writeManifest(root, { effects: [], hello: 'world' }, '.mi');
    const m = readManifest(root, '.mi');
    assert.equal(m.hello, 'world');
    assert.ok(m.installed_at);
    assert.ok(m.updated_at);
  });

  it('inspect marks incomplete transactions and assert blocks install', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-incomplete-'));
    mkdirSync(join(root, '.mi'), { recursive: true });
    writeFileSync(join(root, '.mi', 'manifest.json'), JSON.stringify({
      effects: [],
      transaction: { state: 'incomplete', id: 't1' },
    }, null, 2) + '\n');

    const info = inspectTransaction(root, '.mi');
    assert.equal(info.state, 'incomplete');
    assert.throws(
      () => assertNoIncompleteTransaction(root, '.mi'),
      (err) => err.code === 'INCOMPLETE_TRANSACTION',
    );

    const installer = defineInstaller({
      providers: [createFileSetProvider()],
      config: {
        manifestDir: '.mi',
        lockRoot: join(root, 'locks'),
        files: [{ path: 'a.txt', content: 'x' }],
      },
    });
    assert.throws(() => installer.install({ projectDir: root }), (err) => err.code === 'INCOMPLETE_TRANSACTION');
  });

  it('successful install ends with transaction.state=complete', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-complete-'));
    const installer = defineInstaller({
      providers: [createFileSetProvider()],
      config: {
        manifestDir: '.mi',
        lockRoot: join(root, 'locks'),
        files: [{ path: 'a.txt', content: 'x' }],
      },
    });
    const m = installer.install({ projectDir: root });
    assert.equal(m.transaction.state, 'complete');
    assert.equal(inspectTransaction(root, '.mi').state, 'complete');
    assert.equal(readFileSync(join(root, 'a.txt'), 'utf8'), 'x');
  });
});

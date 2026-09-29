import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { defineInstaller, createFileSetProvider, stableEffectId } from '../src/index.js';

describe('stable effect identity', () => {
  let root;
  afterEach(() => {
    if (root) rmSync(root, { recursive: true, force: true });
    root = undefined;
  });

  it('stableEffectId is deterministic for built-ins', () => {
    assert.equal(stableEffectId('reconcileFileSet', {}), 'reconcileFileSet');
    assert.equal(stableEffectId('jsonMerge', { path: 'a.json' }), 'jsonMerge:a.json');
  });

  it('re-install matches prior reconcileFileSet by stable id', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-effect-id-'));
    const lockRoot = join(root, 'locks');

    const make = (files) => defineInstaller({
      providers: [createFileSetProvider()],
      config: {
        manifestDir: '.mi',
        lockRoot,
        files,
      },
    });

    make([{ path: 'f.txt', content: 'F1' }]).install({ projectDir: root });
    const m2 = make([{ path: 'f.txt', content: 'F2' }]).install({ projectDir: root });
    assert.equal(m2.effects[0].id, 'reconcileFileSet');
    assert.equal(readFileSync(join(root, 'f.txt'), 'utf8'), 'F2');

    writeFileSync(join(root, 'f.txt'), 'user-edit', 'utf8');
    make([{ path: 'f.txt', content: 'F2' }]).uninstall({ projectDir: root });
    assert.equal(readFileSync(join(root, 'f.txt'), 'utf8'), 'user-edit');
  });
});

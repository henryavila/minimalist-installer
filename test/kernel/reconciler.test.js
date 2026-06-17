import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
  classifyFile,
  createReconcileFileSetEffect,
} from '../../src/kernel/reconciler.js';
import { hashContent } from '../../src/hash.js';

describe('reconcileFileSet effect', () => {
  let tempDir;

  afterEach(() => {
    if (tempDir) {
      rmSync(tempDir, { recursive: true, force: true });
      tempDir = undefined;
    }
  });

  const createTempDir = () => {
    tempDir = mkdtempSync(join(tmpdir(), 'as-reconciler-'));
    return tempDir;
  };

  it('removes unmodified installed files and prunes empty parent dirs on revert', () => {
    const basePath = createTempDir();
    const effect = createReconcileFileSetEffect();

    const beforeState = effect.apply({
      basePath,
      desired: [{ path: 'nested/dir/file.txt', content: 'installed content' }],
    });

    effect.revert({ basePath }, beforeState);

    assert.equal(existsSync(join(basePath, 'nested/dir/file.txt')), false);
    assert.equal(existsSync(join(basePath, 'nested/dir')), false);
    assert.equal(existsSync(join(basePath, 'nested')), false);
  });

  it('preserves user-modified files on revert', () => {
    const basePath = createTempDir();
    const effect = createReconcileFileSetEffect();

    const beforeState = effect.apply({
      basePath,
      desired: [
        { path: 'modified.txt', content: 'installed content' },
        { path: 'nested/unmodified.txt', content: 'remove me' },
      ],
    });
    writeFileSync(join(basePath, 'modified.txt'), 'user content', 'utf8');

    effect.revert({ basePath }, beforeState);

    assert.equal(readFileSync(join(basePath, 'modified.txt'), 'utf8'), 'user content');
    assert.equal(existsSync(join(basePath, 'nested/unmodified.txt')), false);
    assert.equal(existsSync(join(basePath, 'nested')), false);
  });

  it('classifies 3-hash file states', () => {
    const installedHash = hashContent('installed');
    const currentHash = hashContent('current');
    const newHash = hashContent('new');

    assert.equal(
      classifyFile({ installedHash, currentHash: installedHash, newHash }),
      'unchanged',
    );
    assert.equal(
      classifyFile({ installedHash, currentHash, newHash: installedHash }),
      'keep-local',
    );
    assert.equal(
      classifyFile({ installedHash, currentHash, newHash }),
      'conflict',
    );
  });

  it('refuses paths that escape basePath on apply and revert', () => {
    // basePath is a nested dir so the `..` escape target stays inside the
    // afterEach-cleaned tempDir (hermetic) instead of polluting the shared tmpdir.
    const root = createTempDir();
    const basePath = join(root, 'install');
    mkdirSync(basePath, { recursive: true });
    const escapeTarget = join(root, 'escapee.txt');
    const effect = createReconcileFileSetEffect();

    assert.throws(
      () => effect.apply({ basePath, desired: [{ path: '../escapee.txt', content: 'x' }] }),
      /outside basePath/,
    );
    assert.equal(existsSync(escapeTarget), false);

    assert.throws(
      () => effect.revert({ basePath }, [{ path: '../escapee.txt', installedHash: hashContent('x') }]),
      /outside basePath/,
    );
  });
});

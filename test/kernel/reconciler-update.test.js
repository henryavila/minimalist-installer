import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { createReconcileFileSetEffect } from '../../src/kernel/reconciler.js';
import { hashContent } from '../../src/hash.js';

describe('reconcileFileSet effect — update (3-hash)', () => {
  let tempDir;

  afterEach(() => {
    if (tempDir) {
      rmSync(tempDir, { recursive: true, force: true });
      tempDir = undefined;
    }
  });

  const dir = () => (tempDir = mkdtempSync(join(tmpdir(), 'ti-recon-upd-')));

  it('overwrites an unmodified installed file with new content on update', () => {
    const basePath = dir();
    const eff = createReconcileFileSetEffect();

    const prev = eff.apply({ basePath, desired: [{ path: 'a.md', content: 'v1' }] });
    const next = eff.apply({
      basePath,
      desired: [{ path: 'a.md', content: 'v2' }],
      previous: prev,
    });

    assert.equal(readFileSync(join(basePath, 'a.md'), 'utf8'), 'v2');
    assert.equal(next[0].installedHash, hashContent('v2'));
  });

  it('keeps a user-modified file on update (no clobber), tracks the original hash, and survives uninstall', () => {
    const basePath = dir();
    const eff = createReconcileFileSetEffect();

    const prev = eff.apply({ basePath, desired: [{ path: 'a.md', content: 'v1' }] });
    writeFileSync(join(basePath, 'a.md'), 'USER', 'utf8');

    const next = eff.apply({
      basePath,
      desired: [{ path: 'a.md', content: 'v2' }],
      previous: prev,
    });

    assert.equal(readFileSync(join(basePath, 'a.md'), 'utf8'), 'USER', 'no clobber on update');
    assert.equal(
      next[0].installedHash,
      hashContent('v1'),
      'tracks the ORIGINAL installed hash, not the user content',
    );

    eff.revert({ basePath }, next);
    assert.equal(
      readFileSync(join(basePath, 'a.md'), 'utf8'),
      'USER',
      'user edits survive uninstall (revert does not reclaim a modified file)',
    );
  });

  it('removes an unmodified orphan (dropped from desired) on update', () => {
    const basePath = dir();
    const eff = createReconcileFileSetEffect();

    const prev = eff.apply({
      basePath,
      desired: [
        { path: 'keep.md', content: 'k' },
        { path: 'nested/drop.md', content: 'd' },
      ],
    });
    const next = eff.apply({
      basePath,
      desired: [{ path: 'keep.md', content: 'k' }],
      previous: prev,
    });

    assert.equal(existsSync(join(basePath, 'nested/drop.md')), false);
    assert.equal(existsSync(join(basePath, 'nested')), false, 'empty parent pruned');
    assert.equal(existsSync(join(basePath, 'keep.md')), true);
    assert.deepEqual(next.map((e) => e.path), ['keep.md']);
  });

  it('keeps a user-modified orphan (never deletes user content) and stops tracking it', () => {
    const basePath = dir();
    const eff = createReconcileFileSetEffect();

    const prev = eff.apply({ basePath, desired: [{ path: 'drop.md', content: 'd' }] });
    writeFileSync(join(basePath, 'drop.md'), 'USER', 'utf8');

    const next = eff.apply({
      basePath,
      desired: [{ path: 'other.md', content: 'o' }],
      previous: prev,
    });

    assert.equal(readFileSync(join(basePath, 'drop.md'), 'utf8'), 'USER');
    assert.deepEqual(next.map((e) => e.path), ['other.md']);
  });

  it('greenfield apply (no previous) writes all desired — back-compat', () => {
    const basePath = dir();
    const eff = createReconcileFileSetEffect();

    const state = eff.apply({ basePath, desired: [{ path: 'a.md', content: 'A' }] });

    assert.equal(readFileSync(join(basePath, 'a.md'), 'utf8'), 'A');
    assert.equal(state[0].installedHash, hashContent('A'));
  });
});

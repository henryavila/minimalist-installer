import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import {
  mkdtempSync, mkdirSync, writeFileSync, readFileSync, symlinkSync, rmSync, unlinkSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { createReconcileFileSetEffect } from '../src/kernel/reconciler.js';
import {
  PathSafetyError,
  writeFileNoFollow,
  openParentNoFollow,
  getPathSafetyBackend,
  resetPathSafetyBackendForTests,
} from '../src/path-safety.js';

describe('path mutation race', () => {
  let root;
  afterEach(() => {
    if (root) rmSync(root, { recursive: true, force: true });
    root = undefined;
    delete process.env.MINIMALIST_INSTALLER_PATH_BACKEND;
    resetPathSafetyBackendForTests();
  });

  it('temp→rename refuses when leaf becomes a symlink before rename', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-path-race-'));
    const basePath = join(root, 'install');
    const outside = join(root, 'outside');
    mkdirSync(join(basePath, 'dir'), { recursive: true });
    mkdirSync(outside, { recursive: true });
    const sentinel = join(outside, 'victim.txt');
    writeFileSync(sentinel, 'SAFE', 'utf8');

    // Place a symlink leaf before the write authority runs (deterministic barrier
    // equivalent to a swap after the last lexical decision).
    symlinkSync(sentinel, join(basePath, 'dir', 'target.txt'));

    assert.throws(
      () => writeFileNoFollow(basePath, 'dir/target.txt', 'PWNED', { atomic: true }),
      (err) => err instanceof PathSafetyError && err.code === 'UNSAFE_PATH_RACE',
    );
    assert.equal(readFileSync(sentinel, 'utf8'), 'SAFE');
  });

  it('reconciler prune does not follow intermediate symlink', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-prune-race-'));
    const basePath = join(root, 'install');
    const outside = join(root, 'outside');
    mkdirSync(basePath, { recursive: true });
    mkdirSync(outside, { recursive: true });
    const sentinel = join(outside, 'keep.txt');
    writeFileSync(sentinel, 'SAFE', 'utf8');

    // First install a real file, then attacker replaces parent with symlink.
    const effect = createReconcileFileSetEffect();
    const before = effect.apply({
      basePath,
      desired: [{ path: 'nested/file.txt', content: 'owned' }],
    });
    assert.equal(readFileSync(join(basePath, 'nested/file.txt'), 'utf8'), 'owned');

    // Replace nested with symlink to outside (simulate race before prune/revert).
    unlinkSync(join(basePath, 'nested/file.txt'));
    // remove empty nested then symlink
    try { rmSync(join(basePath, 'nested'), { recursive: true, force: true }); } catch {}
    symlinkSync(outside, join(basePath, 'nested'));
    // outside has keep.txt; if prune followed symlink it might try nested/file.txt under outside

    assert.throws(
      () => effect.revert({ basePath }, before),
      (err) => err instanceof PathSafetyError && err.code === 'UNSAFE_PATH_RACE',
    );
    assert.equal(readFileSync(sentinel, 'utf8'), 'SAFE');
  });

  it('openParentNoFollow creates parents without following', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-open-parent-'));
    const basePath = join(root, 'install');
    mkdirSync(basePath, { recursive: true });
    const handle = openParentNoFollow(basePath, 'a/b/c.txt', { createParents: true });
    try {
      assert.equal(handle.leafName, 'c.txt');
      assert.ok(typeof handle.parentFd === 'number');
      assert.ok(typeof handle.parentAbs === 'string');
    } finally {
      handle.close();
    }
  });

  it('path-nofollow backend refuses leaf symlink (macOS-class platforms)', () => {
    process.env.MINIMALIST_INSTALLER_PATH_BACKEND = 'path';
    resetPathSafetyBackendForTests();
    assert.equal(getPathSafetyBackend().kind, 'path-nofollow');

    root = mkdtempSync(join(tmpdir(), 'mi-path-backend-'));
    const basePath = join(root, 'install');
    const outside = join(root, 'outside');
    mkdirSync(join(basePath, 'dir'), { recursive: true });
    mkdirSync(outside, { recursive: true });
    const sentinel = join(outside, 'victim.txt');
    writeFileSync(sentinel, 'SAFE', 'utf8');
    symlinkSync(sentinel, join(basePath, 'dir', 'target.txt'));

    assert.throws(
      () => writeFileNoFollow(basePath, 'dir/target.txt', 'PWNED', { atomic: true }),
      (err) => err instanceof PathSafetyError && err.code === 'UNSAFE_PATH_RACE',
    );
    assert.equal(readFileSync(sentinel, 'utf8'), 'SAFE');
  });

  it('path-nofollow backend refuses intermediate symlink on write', () => {
    process.env.MINIMALIST_INSTALLER_PATH_BACKEND = 'path';
    resetPathSafetyBackendForTests();

    root = mkdtempSync(join(tmpdir(), 'mi-path-mid-'));
    const basePath = join(root, 'install');
    const outside = join(root, 'outside');
    mkdirSync(basePath, { recursive: true });
    mkdirSync(outside, { recursive: true });
    const sentinel = join(outside, 'victim.txt');
    writeFileSync(sentinel, 'SAFE', 'utf8');
    symlinkSync(outside, join(basePath, 'nested'));

    assert.throws(
      () => writeFileNoFollow(basePath, 'nested/victim.txt', 'PWNED', { atomic: true }),
      (err) => err instanceof PathSafetyError && err.code === 'UNSAFE_PATH_RACE',
    );
    assert.equal(readFileSync(sentinel, 'utf8'), 'SAFE');
  });
});

import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import {
  mkdtempSync, mkdirSync, writeFileSync, readFileSync, symlinkSync, rmSync, existsSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { createReconcileFileSetEffect } from '../src/kernel/reconciler.js';
import { PathSafetyError } from '../src/path-safety.js';

describe('path confinement (no-follow)', () => {
  let root;
  afterEach(() => {
    if (root) rmSync(root, { recursive: true, force: true });
    root = undefined;
  });

  const setup = () => {
    root = mkdtempSync(join(tmpdir(), 'mi-path-confine-'));
    const basePath = join(root, 'install');
    const outside = join(root, 'outside');
    mkdirSync(basePath, { recursive: true });
    mkdirSync(outside, { recursive: true });
    return { basePath, outside };
  };

  it('refuses intermediate symlink and leaves external sentinel intact', () => {
    const { basePath, outside } = setup();
    const sentinel = join(outside, 'secret.txt');
    writeFileSync(sentinel, 'SAFE', 'utf8');
    symlinkSync(outside, join(basePath, 'nested'));

    const effect = createReconcileFileSetEffect();
    assert.throws(
      () => effect.apply({
        basePath,
        desired: [{ path: 'nested/secret.txt', content: 'PWNED' }],
      }),
      (err) => err instanceof PathSafetyError && err.code === 'UNSAFE_PATH_RACE',
    );
    assert.equal(readFileSync(sentinel, 'utf8'), 'SAFE');
  });

  it('refuses leaf symlink and leaves external sentinel intact', () => {
    const { basePath, outside } = setup();
    mkdirSync(join(basePath, 'dir'), { recursive: true });
    const sentinel = join(outside, 'victim.txt');
    writeFileSync(sentinel, 'SAFE', 'utf8');
    symlinkSync(sentinel, join(basePath, 'dir', 'target.txt'));

    const effect = createReconcileFileSetEffect();
    assert.throws(
      () => effect.apply({
        basePath,
        desired: [{ path: 'dir/target.txt', content: 'RACE-PWNED' }],
      }),
      (err) => err instanceof PathSafetyError && err.code === 'UNSAFE_PATH_RACE',
    );
    assert.equal(readFileSync(sentinel, 'utf8'), 'SAFE');
  });

  it('writes normal paths successfully', () => {
    const { basePath } = setup();
    const effect = createReconcileFileSetEffect();
    effect.apply({
      basePath,
      desired: [{ path: 'ok/file.txt', content: 'hello' }],
    });
    assert.equal(readFileSync(join(basePath, 'ok/file.txt'), 'utf8'), 'hello');
  });
});

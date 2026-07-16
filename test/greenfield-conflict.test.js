import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import {
  mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { createReconcileFileSetEffect } from '../src/kernel/reconciler.js';
import { PathSafetyError } from '../src/path-safety.js';

describe('greenfield conflict', () => {
  let root;
  afterEach(() => {
    if (root) rmSync(root, { recursive: true, force: true });
    root = undefined;
  });

  it('refuses to clobber pre-existing unowned divergent content', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-greenfield-'));
    const basePath = join(root, 'install');
    mkdirSync(basePath, { recursive: true });
    writeFileSync(join(basePath, 'config.json'), 'USER-OWNED', 'utf8');

    const effect = createReconcileFileSetEffect();
    assert.throws(
      () => effect.apply({
        basePath,
        desired: [{ path: 'config.json', content: '{"installed":true}' }],
        previous: [],
      }),
      (err) => err instanceof PathSafetyError && err.code === 'GREENFIELD_CONFLICT',
    );
    assert.equal(readFileSync(join(basePath, 'config.json'), 'utf8'), 'USER-OWNED');
  });

  it('adopts pre-existing content when bytes already match desired', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-greenfield-match-'));
    const basePath = join(root, 'install');
    mkdirSync(basePath, { recursive: true });
    writeFileSync(join(basePath, 'a.txt'), 'same', 'utf8');

    const effect = createReconcileFileSetEffect();
    const before = effect.apply({
      basePath,
      desired: [{ path: 'a.txt', content: 'same' }],
      previous: [],
    });
    assert.equal(before.length, 1);
    assert.equal(readFileSync(join(basePath, 'a.txt'), 'utf8'), 'same');
  });
});

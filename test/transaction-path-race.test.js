import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import {
  mkdtempSync, mkdirSync, writeFileSync, readFileSync, symlinkSync, rmSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { writeManifest } from '../src/manifest.js';
import { PathSafetyError } from '../src/path-safety.js';

describe('transaction path race (manifest write)', () => {
  let root;
  afterEach(() => {
    if (root) rmSync(root, { recursive: true, force: true });
    root = undefined;
  });

  it('refuses manifest write when manifestDir is a symlink escape', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-tx-race-'));
    const project = join(root, 'project');
    const outside = join(root, 'outside');
    mkdirSync(project, { recursive: true });
    mkdirSync(outside, { recursive: true });
    const sentinel = join(outside, 'manifest.json');
    writeFileSync(sentinel, '{"safe":true}\n', 'utf8');
    symlinkSync(outside, join(project, '.mi'));

    assert.throws(
      () => writeManifest(project, { effects: [], evil: true }, '.mi'),
      (err) => err instanceof PathSafetyError || err.code === 'UNSAFE_PATH_RACE' || err.code === 'ELOOP',
    );
    assert.equal(readFileSync(sentinel, 'utf8'), '{"safe":true}\n');
  });
});

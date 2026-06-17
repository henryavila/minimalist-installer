import { describe, it, beforeEach, afterEach } from 'node:test';
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
import { dirname, join } from 'node:path';

import { createJsonMergeEffect } from '../../../src/kernel/effects/json-merge.js';

const readJson = (path) => JSON.parse(readFileSync(path, 'utf8'));

const writeJson = (path, value) => {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(value, null, 2) + '\n', 'utf8');
};

describe('jsonMerge effect', () => {
  let tempDir;

  beforeEach(() => {
    tempDir = mkdtempSync(join(tmpdir(), 'as-json-merge-'));
  });

  afterEach(() => {
    rmSync(tempDir, { recursive: true, force: true });
  });

  it('preserves pre-existing third-party array entries on revert', () => {
    const effect = createJsonMergeEffect();
    const settingsPath = join(tempDir, 'settings.json');
    writeJson(settingsPath, {
      hooks: {
        SessionStart: [
          {
            matcher: '*',
            hooks: [{ type: 'command', command: 'third-party' }],
          },
        ],
      },
    });

    const beforeState = effect.apply({
      basePath: tempDir,
      path: 'settings.json',
      delta: {
        hooks: {
          SessionStart: [
            {
              matcher: '*',
              hooks: [{ type: 'command', command: 'ours' }],
            },
          ],
        },
      },
    });

    effect.revert({ basePath: tempDir, path: 'settings.json' }, beforeState);

    assert.deepEqual(readJson(settingsPath), {
      hooks: {
        SessionStart: [
          {
            matcher: '*',
            hooks: [{ type: 'command', command: 'third-party' }],
          },
        ],
      },
    });
  });

  it('deletes a created file and prunes empty parent dirs when revert empties it', () => {
    const effect = createJsonMergeEffect();
    const filePath = join(tempDir, 'nested/dir/settings.json');

    const beforeState = effect.apply({
      basePath: tempDir,
      path: 'nested/dir/settings.json',
      delta: { hooks: { SessionStart: [{ command: 'ours' }] } },
    });

    effect.revert({ basePath: tempDir, path: 'nested/dir/settings.json' }, beforeState);

    assert.equal(existsSync(filePath), false);
    assert.equal(existsSync(join(tempDir, 'nested/dir')), false);
    assert.equal(existsSync(join(tempDir, 'nested')), false);
  });

  it('subtracts only the applied delta when unrelated edits happen before revert', () => {
    const effect = createJsonMergeEffect();
    const settingsPath = join(tempDir, 'settings.json');
    writeJson(settingsPath, {
      hooks: {
        SessionStart: [{ command: 'third-party-before' }],
      },
    });

    const beforeState = effect.apply({
      basePath: tempDir,
      path: 'settings.json',
      delta: {
        hooks: {
          SessionStart: [{ command: 'ours' }],
        },
        managed: true,
      },
    });
    const current = readJson(settingsPath);
    current.hooks.SessionStart.push({ command: 'third-party-after' });
    current.unrelated = { keep: true };
    writeJson(settingsPath, current);

    effect.revert({ basePath: tempDir, path: 'settings.json' }, beforeState);

    assert.deepEqual(readJson(settingsPath), {
      hooks: {
        SessionStart: [
          { command: 'third-party-before' },
          { command: 'third-party-after' },
        ],
      },
      unrelated: { keep: true },
    });
  });

  it('refuses paths that escape basePath on apply and revert', () => {
    const root = tempDir;
    const basePath = join(root, 'install');
    mkdirSync(basePath, { recursive: true });
    const escapeTarget = join(root, 'escapee.json');
    const effect = createJsonMergeEffect();

    assert.throws(
      () => effect.apply({ basePath, path: '../escapee.json', delta: { x: true } }),
      /outside basePath/,
    );
    assert.equal(existsSync(escapeTarget), false);

    assert.throws(
      () => effect.revert(
        { basePath, path: '../escapee.json' },
        { fileCreated: false, inserts: [], createdContainers: [] },
      ),
      /outside basePath/,
    );
    assert.equal(existsSync(escapeTarget), false);
  });

  it('throws on unparseable existing JSON without clobbering it', () => {
    const effect = createJsonMergeEffect();
    const settingsPath = join(tempDir, 'settings.json');
    writeFileSync(settingsPath, '{not json', 'utf8');

    assert.throws(
      () => effect.apply({ basePath: tempDir, path: 'settings.json', delta: { ok: true } }),
      /Unable to parse JSON/,
    );
    assert.equal(readFileSync(settingsPath, 'utf8'), '{not json');
  });

  it('treats an empty delta as a no-op', () => {
    const effect = createJsonMergeEffect();
    const settingsPath = join(tempDir, 'settings.json');
    writeJson(settingsPath, { existing: true });

    const beforeState = effect.apply({
      basePath: tempDir,
      path: 'settings.json',
      delta: {},
    });
    effect.revert({ basePath: tempDir, path: 'settings.json' }, beforeState);

    assert.deepEqual(beforeState, {
      fileCreated: false,
      inserts: [],
      createdContainers: [],
    });
    assert.equal(readFileSync(settingsPath, 'utf8'), '{\n  "existing": true\n}\n');
  });

  it('refuses scalar overwrites while allowing deep-equal scalars', () => {
    const effect = createJsonMergeEffect();
    const settingsPath = join(tempDir, 'settings.json');
    writeJson(settingsPath, { enabled: true, nested: { name: 'same' } });

    const beforeState = effect.apply({
      basePath: tempDir,
      path: 'settings.json',
      delta: { enabled: true, nested: { name: 'same' } },
    });

    assert.deepEqual(beforeState.inserts, []);
    assert.throws(
      () => effect.apply({ basePath: tempDir, path: 'settings.json', delta: { enabled: false } }),
      /Cannot overwrite existing scalar/,
    );
    assert.deepEqual(readJson(settingsPath), { enabled: true, nested: { name: 'same' } });
  });

  it('round-trips a nested merge while preserving pre-existing bytes', () => {
    const effect = createJsonMergeEffect();
    const settingsPath = join(tempDir, 'settings.json');
    const original = {
      hooks: {
        SessionStart: [{ command: 'third-party' }],
        Other: [{ command: 'keep' }],
      },
      options: {
        mode: 'stable',
        nested: { keep: true },
      },
    };
    const originalBytes = JSON.stringify(original, null, 2) + '\n';
    writeFileSync(settingsPath, originalBytes, 'utf8');

    const beforeState = effect.apply({
      basePath: tempDir,
      path: 'settings.json',
      delta: {
        hooks: {
          SessionStart: [{ command: 'ours' }],
        },
        options: {
          nested: { added: { list: [1, { two: true }] } },
        },
      },
    });

    effect.revert({ basePath: tempDir, path: 'settings.json' }, beforeState);

    assert.equal(readFileSync(settingsPath, 'utf8'), originalBytes);
  });

  it('does not duplicate array items when the same array delta is applied twice', () => {
    const effect = createJsonMergeEffect();
    const settingsPath = join(tempDir, 'settings.json');
    writeJson(settingsPath, { hooks: { SessionStart: [] } });
    const delta = { hooks: { SessionStart: [{ command: 'ours' }] } };

    const firstState = effect.apply({ basePath: tempDir, path: 'settings.json', delta });
    const secondState = effect.apply({ basePath: tempDir, path: 'settings.json', delta });

    assert.deepEqual(readJson(settingsPath), {
      hooks: { SessionStart: [{ command: 'ours' }] },
    });
    assert.deepEqual(firstState.inserts, [
      {
        kind: 'arrayItem',
        path: ['hooks', 'SessionStart'],
        value: { command: 'ours' },
      },
    ]);
    assert.deepEqual(secondState.inserts, []);
  });
});

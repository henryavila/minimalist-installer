import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import {
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { defineInstaller, createFileSetProvider } from '../../src/index.js';

describe('defineInstaller (two-tier config)', () => {
  let tempDir;

  afterEach(() => {
    if (tempDir) {
      rmSync(tempDir, { recursive: true, force: true });
      tempDir = undefined;
    }
  });

  const dir = () => (tempDir = mkdtempSync(join(tmpdir(), 'ti-define-')));

  it('wires built-in effects + providers + declarative config into a ready installer', () => {
    const projectDir = dir();
    const installer = defineInstaller({
      providers: [createFileSetProvider()],
      config: { manifestDir: '.ti-test', files: [{ path: 'a.md', content: 'A' }] },
    });

    installer.install({ projectDir });
    assert.equal(readFileSync(join(projectDir, 'a.md'), 'utf8'), 'A');

    installer.uninstall({ projectDir });
    assert.deepEqual(readdirSync(projectDir), [], 'round-trip to empty');
  });

  it('registers the 4 built-in effect types by default (P4)', () => {
    const installer = defineInstaller({
      providers: [createFileSetProvider()],
      config: {},
    });

    assert.deepEqual(
      installer.registry.list().sort(),
      ['jsonMerge', 'legacyPrune', 'reconcileFileSet', 'refcount'],
    );
  });

  it('registers a consumer custom effect type alongside the built-ins (runtime-layer escape hatch)', () => {
    const custom = { type: 'customThing', apply: () => ({}), revert: () => {} };
    const installer = defineInstaller({
      effects: [custom],
      providers: [createFileSetProvider()],
      config: {},
    });

    assert.equal(installer.registry.has('customThing'), true);
    assert.equal(installer.registry.has('reconcileFileSet'), true);
  });

  it('throws when no provider is supplied', () => {
    assert.throws(
      () => defineInstaller({ providers: [], config: {} }),
      /at least one provider/,
    );
  });
});

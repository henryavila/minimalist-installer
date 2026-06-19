import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import {
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { createEffectRegistry } from '../../src/kernel/effect.js';
import { createReconcileFileSetEffect } from '../../src/kernel/reconciler.js';
import { createFileSetProvider } from '../../src/provider.js';
import { createDriver } from '../../src/driver.js';

describe('driver install/uninstall (round-trip)', () => {
  let tempDir;

  afterEach(() => {
    if (tempDir) {
      rmSync(tempDir, { recursive: true, force: true });
      tempDir = undefined;
    }
  });

  const setup = () => {
    tempDir = mkdtempSync(join(tmpdir(), 'ti-driver-'));
    const registry = createEffectRegistry();
    registry.register(createReconcileFileSetEffect());
    const driver = createDriver({
      registry,
      providers: [createFileSetProvider()],
      manifestDir: '.ti-test',
    });
    return { dir: tempDir, driver };
  };

  it('install writes the provider file set and journals each effect in the manifest', () => {
    const { dir, driver } = setup();

    driver.install(
      { files: [{ path: 'skills/a.md', content: 'A' }] },
      { projectDir: dir },
    );

    assert.equal(readFileSync(join(dir, 'skills/a.md'), 'utf8'), 'A');
    const manifest = JSON.parse(
      readFileSync(join(dir, '.ti-test/manifest.json'), 'utf8'),
    );
    assert.equal(manifest.effects.length, 1);
    assert.equal(manifest.effects[0].type, 'reconcileFileSet');
  });

  it('uninstall reverts to a byte-for-byte baseline (round-trip)', () => {
    const { dir, driver } = setup();

    driver.install(
      {
        files: [
          { path: 'skills/a.md', content: 'A' },
          { path: 'cli/b.sh', content: 'B' },
        ],
      },
      { projectDir: dir },
    );
    driver.uninstall({ projectDir: dir });

    assert.deepEqual(readdirSync(dir), [], 'install root returns to empty');
  });

  it('uninstall preserves a user-modified installed file (no clobber)', () => {
    const { dir, driver } = setup();

    driver.install(
      { files: [{ path: 'skills/a.md', content: 'A' }] },
      { projectDir: dir },
    );
    writeFileSync(join(dir, 'skills/a.md'), 'USER EDIT', 'utf8');

    driver.uninstall({ projectDir: dir });

    assert.equal(readFileSync(join(dir, 'skills/a.md'), 'utf8'), 'USER EDIT');
  });

  it('uninstall on a never-installed root is a no-op', () => {
    const { dir, driver } = setup();
    driver.uninstall({ projectDir: dir });
    assert.deepEqual(readdirSync(dir), []);
  });

  it('install throws when a provider emits an unregistered effect type', () => {
    tempDir = mkdtempSync(join(tmpdir(), 'ti-driver-'));
    const registry = createEffectRegistry();
    const ghostProvider = { plan: () => [{ type: 'no-such-effect', args: {} }] };
    const driver = createDriver({
      registry,
      providers: [ghostProvider],
      manifestDir: '.ti-test',
    });

    assert.throws(
      () => driver.install({}, { projectDir: tempDir }),
      /unregistered effect type "no-such-effect"/,
    );
  });
});

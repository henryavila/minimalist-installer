import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import {
  existsSync,
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

describe('driver re-install / update', () => {
  let tempDir;

  afterEach(() => {
    if (tempDir) {
      rmSync(tempDir, { recursive: true, force: true });
      tempDir = undefined;
    }
  });

  const setup = () => {
    tempDir = mkdtempSync(join(tmpdir(), 'ti-driver-upd-'));
    const registry = createEffectRegistry();
    registry.register(createReconcileFileSetEffect());
    const driver = createDriver({
      registry,
      providers: [createFileSetProvider()],
      manifestDir: '.ti-test',
    });
    return { dir: tempDir, driver };
  };

  it('update applies the new file set, preserves user edits, removes unmodified orphans', () => {
    const { dir, driver } = setup();

    driver.install(
      {
        files: [
          { path: 'a.md', content: 'v1' },
          { path: 'drop.md', content: 'd' },
        ],
      },
      { projectDir: dir },
    );
    writeFileSync(join(dir, 'a.md'), 'USER', 'utf8'); // user edits a tracked file

    driver.install(
      {
        files: [
          { path: 'a.md', content: 'v2' },
          { path: 'new.md', content: 'n' },
        ],
      },
      { projectDir: dir },
    );

    assert.equal(readFileSync(join(dir, 'a.md'), 'utf8'), 'USER', 'user edit preserved');
    assert.equal(readFileSync(join(dir, 'new.md'), 'utf8'), 'n', 'new file added');
    assert.equal(existsSync(join(dir, 'drop.md')), false, 'unmodified orphan removed');

    const manifest = JSON.parse(
      readFileSync(join(dir, '.ti-test/manifest.json'), 'utf8'),
    );
    assert.equal(manifest.effects.length, 1, 'no journal duplication on re-install');
    const paths = manifest.effects[0].beforeState.map((e) => e.path).sort();
    assert.deepEqual(paths, ['a.md', 'new.md'], 'manifest reflects the new set');
  });

  it('uninstall after an update returns to baseline', () => {
    const { dir, driver } = setup();

    driver.install({ files: [{ path: 'a.md', content: 'v1' }] }, { projectDir: dir });
    driver.install(
      { files: [{ path: 'a.md', content: 'v2' }, { path: 'b.md', content: 'b' }] },
      { projectDir: dir },
    );
    driver.uninstall({ projectDir: dir });

    assert.deepEqual(readdirSync(dir), [], 'install root returns to empty');
  });
});

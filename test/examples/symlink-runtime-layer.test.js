import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import {
  existsSync,
  lstatSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  readlinkSync,
  rmSync,
  symlinkSync,
  unlinkSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { defineInstaller } from '../../src/index.js';
import {
  createSymlinkEffect,
  createSymlinkProvider,
} from '../../examples/symlink-runtime-layer.js';

describe('runtime-layer worked example: a consumer custom symlink effect', () => {
  let tempDir;

  afterEach(() => {
    if (tempDir) {
      rmSync(tempDir, { recursive: true, force: true });
      tempDir = undefined;
    }
  });

  const dir = () => (tempDir = mkdtempSync(join(tmpdir(), 'ti-symlink-')));

  const build = () =>
    defineInstaller({
      effects: [createSymlinkEffect()], // <-- new reversible type, no kernel change
      providers: [createSymlinkProvider()],
      config: {
        manifestDir: '.ti-test',
        links: [{ from: 'bin/tool', to: 'libexec/tool.js' }],
      },
    });

  it('registers the new effect type alongside the built-ins', () => {
    const installer = build();
    assert.equal(installer.registry.has('symlink'), true);
    assert.equal(installer.registry.has('reconcileFileSet'), true);
  });

  it('round-trips: install creates the symlink + journals it; uninstall reverts to empty', () => {
    const projectDir = dir();
    const installer = build();

    installer.install({ projectDir });
    const linkPath = join(projectDir, 'bin/tool');
    assert.ok(lstatSync(linkPath).isSymbolicLink(), 'symlink created');
    assert.equal(readlinkSync(linkPath), 'libexec/tool.js');

    const manifest = JSON.parse(
      readFileSync(join(projectDir, '.ti-test/manifest.json'), 'utf8'),
    );
    assert.equal(manifest.effects[0].type, 'symlink');

    installer.uninstall({ projectDir });
    assert.equal(existsSync(linkPath), false, 'symlink removed');
    assert.deepEqual(readdirSync(projectDir), [], 'round-trip to empty');
  });

  it('uninstall preserves a symlink the user repointed (no proof-less deletion)', () => {
    const projectDir = dir();
    const installer = build();

    installer.install({ projectDir });
    const linkPath = join(projectDir, 'bin/tool');
    unlinkSync(linkPath);
    symlinkSync('somewhere-else', linkPath); // user repoints it

    installer.uninstall({ projectDir });

    assert.ok(lstatSync(linkPath).isSymbolicLink(), 'user-repointed symlink kept');
    assert.equal(readlinkSync(linkPath), 'somewhere-else');
  });
});

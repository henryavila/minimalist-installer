import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { defineInstaller, createFileSetProvider, JOURNAL_VERSION } from '../src/index.js';

describe('journal v2', () => {
  let root;
  afterEach(() => {
    if (root) rmSync(root, { recursive: true, force: true });
    root = undefined;
  });

  it('records journalVersion and stable ids on effects', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-jv2-'));
    const installer = defineInstaller({
      providers: [createFileSetProvider()],
      config: {
        manifestDir: '.mi',
        lockRoot: join(root, 'locks'),
        files: [{ path: 'x.md', content: 'X' }],
      },
    });
    const m = installer.install({ projectDir: root });
    assert.equal(m.journalVersion, JOURNAL_VERSION);
    assert.ok(m.effects.every((e) => typeof e.id === 'string' && e.id.length > 0));
    assert.equal(m.effects[0].id, 'reconcileFileSet');
    const disk = JSON.parse(readFileSync(join(root, '.mi/manifest.json'), 'utf8'));
    assert.equal(disk.journalVersion, JOURNAL_VERSION);
  });
});

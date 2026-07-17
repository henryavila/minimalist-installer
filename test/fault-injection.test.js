import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import { mkdtempSync, existsSync, rmSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  defineInstaller,
  createFileSetProvider,
  inspectTransaction,
  describeRecovery,
} from '../src/index.js';

describe('fault injection', () => {
  let root;
  afterEach(() => {
    if (root) rmSync(root, { recursive: true, force: true });
    root = undefined;
  });

  it('late effect failure leaves incomplete transaction and refuses retry without recovery', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-fault-'));
    const boom = {
      type: 'boom',
      apply() { throw new Error('injected'); },
      revert() {},
    };
    const installer = defineInstaller({
      providers: [
        createFileSetProvider(),
        { plan: () => [{ type: 'boom', args: {} }] },
      ],
      effects: [boom],
      config: {
        manifestDir: '.mi',
        lockRoot: join(root, 'locks'),
        files: [{ path: 'skills/a.md', content: 'A' }],
      },
    });

    assert.throws(() => installer.install({ projectDir: root }), /injected/);
    assert.equal(existsSync(join(root, 'skills/a.md')), true);
    assert.equal(inspectTransaction(root, '.mi').state, 'incomplete');

    // F-001: the successful file-set apply must be durable on the incomplete journal
    const onDisk = JSON.parse(readFileSync(join(root, '.mi/manifest.json'), 'utf8'));
    assert.equal(onDisk.transaction.journalMode, 'per-effect');
    assert.equal(onDisk.effects.length, 1);
    assert.equal(onDisk.effects[0].type, 'reconcileFileSet');
    assert.equal(describeRecovery(root, '.mi').effectCount, 1);
    assert.equal(describeRecovery(root, '.mi').durablePerEffect, true);

    assert.throws(
      () => installer.install({ projectDir: root }),
      (err) => err.code === 'INCOMPLETE_TRANSACTION',
    );
  });
});

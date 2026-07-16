import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import { mkdtempSync, mkdirSync, readFileSync, existsSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { defineInstaller, createFileSetProvider, inspectTransaction } from '../src/index.js';

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

    assert.throws(
      () => installer.install({ projectDir: root }),
      (err) => err.code === 'INCOMPLETE_TRANSACTION',
    );
  });
});

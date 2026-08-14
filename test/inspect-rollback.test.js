import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describeRecovery, inspectTransaction } from '../src/recovery.js';

describe('inspect rollback (read-only)', () => {
  let root;
  afterEach(() => {
    if (root) rmSync(root, { recursive: true, force: true });
    root = undefined;
  });

  it('describeRecovery is read-only and reports journal version', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-inspect-'));
    mkdirSync(join(root, '.mi'), { recursive: true });
    writeFileSync(join(root, '.mi', 'manifest.json'), JSON.stringify({
      journalVersion: 2,
      effects: [{ type: 'reconcileFileSet', id: 'reconcileFileSet', beforeState: [] }],
      transaction: { state: 'complete' },
    }, null, 2) + '\n');

    const before = inspectTransaction(root, '.mi');
    const desc = describeRecovery(root, '.mi');
    assert.equal(desc.state, 'complete');
    assert.equal(desc.effectCount, 1);
    assert.equal(desc.journalVersion, 2);
    // Pre-U / hand-written manifests without journalMode are not durablePerEffect
    assert.equal(desc.durablePerEffect, false);
    assert.equal(desc.journalMode, null);
    // No mutation: re-inspect identical state.
    assert.equal(inspectTransaction(root, '.mi').state, before.state);
  });

  it('describeRecovery reports durablePerEffect when journalMode is per-effect', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-inspect-durable-'));
    mkdirSync(join(root, '.mi'), { recursive: true });
    writeFileSync(join(root, '.mi', 'manifest.json'), JSON.stringify({
      journalVersion: 2,
      effects: [
        { type: 'reconcileFileSet', id: 'reconcileFileSet', beforeState: [] },
        { type: 'jsonMerge', id: 'jsonMerge:x', beforeState: {} },
      ],
      transaction: {
        state: 'incomplete',
        journalMode: 'per-effect',
        appliedCount: 2,
      },
    }, null, 2) + '\n');

    const desc = describeRecovery(root, '.mi');
    assert.equal(desc.state, 'incomplete');
    assert.equal(desc.effectCount, 2);
    assert.equal(desc.appliedCount, 2);
    assert.equal(desc.durablePerEffect, true);
    assert.deepEqual(desc.effectIds, ['reconcileFileSet', 'jsonMerge:x']);
  });
});

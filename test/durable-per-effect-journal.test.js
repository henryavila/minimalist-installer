import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import {
  mkdtempSync,
  readFileSync,
  existsSync,
  rmSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  defineInstaller,
  createFileSetProvider,
  createDriver,
  createEffectRegistry,
  createReconcileFileSetEffect,
  inspectTransaction,
  describeRecovery,
  readJournaledEffects,
  assertNoIncompleteTransaction,
  readManifest,
} from '../src/index.js';

/**
 * F-001 — durable per-effect journaling.
 *
 * After each successful apply the incomplete journal must list exactly the
 * applied effects so crash/SIGKILL never leaves disk ownership without a
 * journal entry. assertNoIncomplete still fails closed; describeRecovery
 * reports effectCount = N for the journaled set.
 */
describe('F-001 durable per-effect journal', () => {
  let root;
  afterEach(() => {
    if (root) rmSync(root, { recursive: true, force: true });
    root = undefined;
  });

  const makeCountingBoom = (failAfter) => {
    let applied = 0;
    return {
      type: 'step',
      apply(args) {
        applied += 1;
        if (applied > failAfter) {
          throw new Error(`injected-fail-after-${failAfter}`);
        }
        return { label: args.label, n: applied };
      },
      revert() {},
    };
  };

  const installWithSteps = (labels, { failAfter, lockRoot, manifestDir = '.mi' }) => {
    const step = makeCountingBoom(failAfter ?? Infinity);
    const registry = createEffectRegistry();
    registry.register(createReconcileFileSetEffect());
    registry.register(step);
    const driver = createDriver({
      registry,
      providers: [
        createFileSetProvider(),
        {
          plan: () => labels.map((label) => ({ type: 'step', args: { label } })),
        },
      ],
      manifestDir,
      lockRoot,
    });
    return driver.install(
      {
        files: labels.map((label) => ({
          path: `artifacts/${label}.txt`,
          content: label,
        })),
      },
      { projectDir: root },
    );
  };

  it('fail after Nth effect → on-disk journal has exactly N applied effects', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-durable-'));
    const lockRoot = join(root, 'locks');
    // Plan: 1 reconcileFileSet + 3 step effects. Fail on the 3rd step
    // (after 1 file-set + 2 steps = 3 applied journal entries; boom is 4th plan
    // slot... wait: failAfter counts only `step` applies.
    // We want: file-set applies, step-a applies, step-b applies, step-c throws.
    // Journal should contain reconcileFileSet + step-a + step-b = 3 entries.
    const failAfter = 2;

    assert.throws(
      () => installWithSteps(['a', 'b', 'c'], { failAfter, lockRoot }),
      /injected-fail-after-2/,
    );

    const onDisk = JSON.parse(
      readFileSync(join(root, '.mi/manifest.json'), 'utf8'),
    );
    assert.equal(onDisk.transaction.state, 'incomplete');
    assert.equal(onDisk.transaction.journalMode, 'per-effect');
    // reconcileFileSet + step a + step b (step c never journaled)
    assert.equal(onDisk.effects.length, 3);
    assert.equal(onDisk.transaction.appliedCount, 3);
    assert.equal(onDisk.effects[0].type, 'reconcileFileSet');
    assert.equal(onDisk.effects[1].type, 'step');
    assert.equal(onDisk.effects[1].beforeState.label, 'a');
    assert.equal(onDisk.effects[2].type, 'step');
    assert.equal(onDisk.effects[2].beforeState.label, 'b');

    // Applied ownership is on disk for journaled steps' artifacts via file-set
    assert.equal(existsSync(join(root, 'artifacts/a.txt')), true);
    assert.equal(existsSync(join(root, 'artifacts/b.txt')), true);
    assert.equal(existsSync(join(root, 'artifacts/c.txt')), true);
  });

  it('assertNoIncomplete still fails closed after partial durable journal', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-durable-closed-'));
    const lockRoot = join(root, 'locks');

    assert.throws(
      () => installWithSteps(['a', 'b'], { failAfter: 1, lockRoot }),
      /injected-fail-after-1/,
    );

    assert.throws(
      () => assertNoIncompleteTransaction(root, '.mi'),
      (err) => err.code === 'INCOMPLETE_TRANSACTION',
    );

    const installer = defineInstaller({
      providers: [createFileSetProvider()],
      config: {
        manifestDir: '.mi',
        lockRoot,
        files: [{ path: 'x.txt', content: 'x' }],
      },
    });
    assert.throws(
      () => installer.install({ projectDir: root }),
      (err) => err.code === 'INCOMPLETE_TRANSACTION',
    );
    assert.throws(
      () => installer.uninstall({ projectDir: root }),
      (err) => err.code === 'INCOMPLETE_TRANSACTION',
    );
  });

  it('describeRecovery shows effectCount = N and durablePerEffect trust', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-durable-desc-'));
    const lockRoot = join(root, 'locks');

    assert.throws(
      () => installWithSteps(['a', 'b', 'c'], { failAfter: 1, lockRoot }),
      /injected-fail-after-1/,
    );

    const desc = describeRecovery(root, '.mi');
    assert.equal(desc.state, 'incomplete');
    // reconcileFileSet + step a
    assert.equal(desc.effectCount, 2);
    assert.equal(desc.appliedCount, 2);
    assert.equal(desc.journalMode, 'per-effect');
    assert.equal(desc.durablePerEffect, true);
    assert.ok(desc.effectIds.includes('reconcileFileSet'));

    const journaled = readJournaledEffects(root, '.mi');
    assert.equal(journaled.state, 'incomplete');
    assert.equal(journaled.durablePerEffect, true);
    assert.equal(journaled.effects.length, 2);
  });

  it('successful install still ends complete with journalMode per-effect', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-durable-ok-'));
    const lockRoot = join(root, 'locks');
    const m = installWithSteps(['a'], { failAfter: Infinity, lockRoot });
    assert.equal(m.transaction.state, 'complete');
    assert.equal(m.transaction.journalMode, 'per-effect');
    assert.equal(inspectTransaction(root, '.mi').state, 'complete');
    assert.equal(readManifest(root, '.mi').transaction.appliedCount, 2);
  });

  it('mid-install crash simulation: prior journal replaced by applied new effects only', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-durable-prior-'));
    const lockRoot = join(root, 'locks');

    // Complete first install
    installWithSteps(['old'], { failAfter: Infinity, lockRoot });
    assert.equal(inspectTransaction(root, '.mi').state, 'complete');

    // Second install fails after first step of new plan
    assert.throws(
      () => installWithSteps(['new1', 'new2'], { failAfter: 1, lockRoot }),
      /injected-fail-after-1/,
    );

    const onDisk = readManifest(root, '.mi');
    assert.equal(onDisk.transaction.state, 'incomplete');
    // Journal must reflect THIS install's applied effects, not stale prior-only
    // ownership while new file-set already mutated disk (the pre-F-001 bug).
    assert.equal(onDisk.effects.length, 2);
    assert.equal(onDisk.effects[0].type, 'reconcileFileSet');
    assert.equal(onDisk.effects[1].beforeState.label, 'new1');
    // Prior-only "old" step must not be the sole remaining journal content
    assert.ok(
      !onDisk.effects.some((e) => e.beforeState?.label === 'old'),
      'stale prior step must not remain as sole journal after new applies',
    );
  });
});

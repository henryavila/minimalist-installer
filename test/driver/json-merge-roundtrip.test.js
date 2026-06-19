import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import {
  mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync, existsSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { defineInstaller } from '../../src/index.js';

// Regression guard: the built-in jsonMerge effect must round-trip THROUGH the
// Driver. The journal persists only { type, beforeState }, and replayReverse
// calls revert({ basePath, manifestDir }, beforeState) — it never re-supplies the
// apply args. jsonMerge therefore has to carry `path` in its before-state (it
// used to read `path` from the revert ctx, which the Driver does not provide, so
// uninstall crashed with "path must be a string"). reconcileFileSet always
// worked because its revert needs only basePath + before-state; this exercises
// jsonMerge the same way.
describe('driver round-trip — built-in jsonMerge', () => {
  let tempDir;

  afterEach(() => {
    if (tempDir) {
      rmSync(tempDir, { recursive: true, force: true });
      tempDir = undefined;
    }
  });

  // A provider that merges a SessionStart-style command entry into settings.json.
  const hookMergeProvider = (command) => ({
    plan(_config, { basePath }) {
      return [{
        type: 'jsonMerge',
        args: {
          basePath,
          path: '.claude/settings.json',
          delta: { hooks: { SessionStart: [{ matcher: '*', hooks: [{ type: 'command', command }] }] } },
        },
      }];
    },
  });

  it('merges on install and subtracts exactly the delta on uninstall (third-party survives)', () => {
    tempDir = mkdtempSync(join(tmpdir(), 'ti-jsonmerge-'));
    mkdirSync(join(tempDir, '.claude'), { recursive: true });
    const settingsPath = join(tempDir, '.claude', 'settings.json');

    const thirdParty = { type: 'command', command: '/usr/local/bin/other.sh' };
    const baseline = { theme: 'dark', hooks: { SessionStart: [{ matcher: '*', hooks: [thirdParty] }] } };
    writeFileSync(settingsPath, JSON.stringify(baseline, null, 2) + '\n', 'utf8');
    const baselineStr = readFileSync(settingsPath, 'utf8');

    const installer = defineInstaller({
      providers: [hookMergeProvider('/abs/version-check.sh')],
      config: { manifestDir: '.ti-test' },
    });

    installer.install({ projectDir: tempDir });

    const merged = JSON.parse(readFileSync(settingsPath, 'utf8'));
    const allHooks = merged.hooks.SessionStart.flatMap((e) => e.hooks);
    assert.ok(allHooks.some((h) => h.command === thirdParty.command), 'third-party preserved');
    assert.ok(allHooks.some((h) => h.command === '/abs/version-check.sh'), 'our command added');

    installer.uninstall({ projectDir: tempDir }); // must NOT throw on jsonMerge.revert

    assert.equal(readFileSync(settingsPath, 'utf8'), baselineStr, 'settings.json restored to baseline');
  });

  it('deletes an installer-created settings.json on uninstall', () => {
    tempDir = mkdtempSync(join(tmpdir(), 'ti-jsonmerge-'));
    const settingsPath = join(tempDir, '.claude', 'settings.json');

    const installer = defineInstaller({
      providers: [hookMergeProvider('/abs/version-check.sh')],
      config: { manifestDir: '.ti-test' },
    });

    installer.install({ projectDir: tempDir });
    assert.ok(existsSync(settingsPath), 'settings.json created by the merge');

    installer.uninstall({ projectDir: tempDir });
    assert.equal(existsSync(settingsPath), false, 'installer-created settings.json removed');
  });

  // Regression guard for the UPDATE path: a second install re-touches an entry that
  // is already present, so jsonMerge.apply inserts nothing THIS run. The latest
  // journal must still own the merge (carried forward from the prior before-state via
  // the Driver-threaded `previous`), or uninstall — which replays only the latest
  // journal — leaves our command behind.
  it('survives an UPDATE (re-install) before uninstall — third-party survives, our merge is removed', () => {
    tempDir = mkdtempSync(join(tmpdir(), 'ti-jsonmerge-'));
    mkdirSync(join(tempDir, '.claude'), { recursive: true });
    const settingsPath = join(tempDir, '.claude', 'settings.json');

    const thirdParty = { type: 'command', command: '/usr/local/bin/other.sh' };
    const baseline = { theme: 'dark', hooks: { SessionStart: [{ matcher: '*', hooks: [thirdParty] }] } };
    writeFileSync(settingsPath, JSON.stringify(baseline, null, 2) + '\n', 'utf8');
    const baselineStr = readFileSync(settingsPath, 'utf8');

    const installer = defineInstaller({
      providers: [hookMergeProvider('/abs/version-check.sh')],
      config: { manifestDir: '.ti-test' },
    });

    installer.install({ projectDir: tempDir });
    installer.install({ projectDir: tempDir }); // UPDATE — entry already present, inserts nothing this apply
    installer.uninstall({ projectDir: tempDir });

    assert.equal(
      readFileSync(settingsPath, 'utf8'), baselineStr,
      'settings.json restored to baseline after update→uninstall (latest journal still owns the merge)',
    );
  });

  it('deletes an installer-created settings.json after an UPDATE before uninstall', () => {
    tempDir = mkdtempSync(join(tmpdir(), 'ti-jsonmerge-'));
    const settingsPath = join(tempDir, '.claude', 'settings.json');

    const installer = defineInstaller({
      providers: [hookMergeProvider('/abs/version-check.sh')],
      config: { manifestDir: '.ti-test' },
    });

    installer.install({ projectDir: tempDir });
    installer.install({ projectDir: tempDir }); // UPDATE
    assert.ok(existsSync(settingsPath), 'settings.json present after update');

    installer.uninstall({ projectDir: tempDir });
    assert.equal(existsSync(settingsPath), false,
      'installer-created settings.json removed after update→uninstall (fileCreated carried forward)');
  });
});

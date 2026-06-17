import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, it } from 'node:test';
import { strict as assert } from 'node:assert';
import { createEffectRegistry } from '../../src/kernel/effect.js';
import { readManifest, writeManifest } from '../../src/manifest.js';
import { readEffects, recordEffect, replayReverse } from '../../src/kernel/journal.js';

describe('effect journal', () => {
  it('records and reads back before-state through the manifest round-trip', () => {
    const projectDir = mkdtempSync(join(tmpdir(), 'atomic-skills-journal-'));
    const manifest = {
      skill: 'example',
      version: 1,
    };
    const beforeState = {
      hadValue: true,
      previous: {
        nested: ['alpha', 'beta'],
      },
    };

    const updatedManifest = recordEffect(manifest, { type: 'set-config', beforeState });
    writeManifest(projectDir, updatedManifest);

    const persistedManifest = readManifest(projectDir);

    assert.deepStrictEqual(readEffects(persistedManifest), [
      { type: 'set-config', beforeState },
    ]);
    assert.deepStrictEqual(readEffects(manifest), []);
  });

  it('replays reverts in reverse record order', () => {
    const calls = [];
    const registry = createEffectRegistry();

    for (const type of ['e1', 'e2', 'e3']) {
      registry.register({
        type,
        apply() {
          return undefined;
        },
        revert(ctx, beforeState) {
          ctx.calls.push(beforeState.marker);
        },
      });
    }

    const manifest = [
      { type: 'e1', beforeState: { marker: 'e1' } },
      { type: 'e2', beforeState: { marker: 'e2' } },
      { type: 'e3', beforeState: { marker: 'e3' } },
    ].reduce((current, effect) => recordEffect(current, effect), {});

    replayReverse(manifest, { calls }, registry);

    assert.deepStrictEqual(calls, ['e3', 'e2', 'e1']);
  });

  it('treats old manifests without effects as empty journals', () => {
    const registry = createEffectRegistry();
    const manifest = {
      skill: 'old-skill',
    };

    assert.deepStrictEqual(readEffects(manifest), []);
    assert.doesNotThrow(() => replayReverse(manifest, {}, registry));
  });

  it('throws when a recorded effect type is not registered', () => {
    const manifest = recordEffect({}, { type: 'missing', beforeState: { value: 1 } });

    assert.throws(
      () => replayReverse(manifest, {}, createEffectRegistry()),
      /unknown effect type "missing"/,
    );
  });

  it('treats a null manifest (never installed) as an empty journal', () => {
    // readManifest returns null when no manifest file exists; revert driven off
    // that must be a no-op, not a crash.
    assert.deepStrictEqual(readEffects(null), []);
    assert.doesNotThrow(() => replayReverse(null, {}, createEffectRegistry()));
  });
});

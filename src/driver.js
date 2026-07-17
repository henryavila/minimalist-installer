import {
  readManifest,
  writeManifest,
  removeManifest,
  MANIFEST_DIR,
} from './manifest.js';
import { readEffects, recordEffect, replayReverse, stableEffectId, JOURNAL_VERSION } from './kernel/journal.js';
import { acquireInstallLocks } from './lock.js';
import { assertNoIncompleteTransaction } from './recovery.js';

// The Driver is identical for every consumer. It runs the configured providers to
// emit effects, applies each effect, journals its before-state on the manifest,
// and persists the manifest. Uninstall replays the journal in reverse (the
// structural uninstall) and removes the manifest — no consumer writes revert logic.
//
// Matching prior effects: prefer stable `id` (journal v2); fall back to
// (type, occurrence order) for v1 manifests.
export const createDriver = ({
  registry,
  providers,
  manifestDir = MANIFEST_DIR,
  lockRoot,
  resourceIdentities,
} = {}) => {
  const planEffects = (config, projectDir) => {
    const planCtx = { basePath: projectDir, manifestDir };
    return providers.flatMap((provider) => provider.plan(config, planCtx));
  };

  const resolveExtraIdentities = (projectDir, config) => {
    if (typeof resourceIdentities === 'function') {
      return resourceIdentities({ projectDir, config, manifestDir }) ?? [];
    }
    return resourceIdentities ?? [];
  };

  return {
    install(config, { projectDir }) {
      assertNoIncompleteTransaction(projectDir, manifestDir);

      const extra = resolveExtraIdentities(projectDir, config);
      const locks = acquireInstallLocks({ projectDir, extra, lockRoot });
      try {
        const priorById = new Map();
        const priorByType = new Map();
        const prior = readManifest(projectDir, manifestDir);
        if (prior) {
          for (const entry of readEffects(prior)) {
            const { type, beforeState, id } = entry;
            if (id != null) {
              priorById.set(id, beforeState);
            }
            if (!priorByType.has(type)) priorByType.set(type, []);
            priorByType.get(type).push(beforeState);
          }
        }

        const cursor = new Map();
        // Incomplete marker preserves prior effects so a crash before the first
        // mutation still leaves a recoverable journal. Once the first new effect
        // applies, the on-disk journal is rebuilt from applied effects only
        // (durable per-effect flush — F-001). journalMode marks post-U trust.
        const txId = `${Date.now()}-${process.pid}`;
        const startedAt = new Date().toISOString();
        let manifest = {
          journalVersion: JOURNAL_VERSION,
          effects: prior ? [...readEffects(prior)] : [],
          transaction: {
            id: txId,
            state: 'incomplete',
            journalMode: 'per-effect',
            startedAt,
            appliedCount: 0,
          },
        };
        writeManifest(projectDir, manifest, manifestDir);

        // Rebuild journal for this install (in memory until first successful apply).
        manifest = { ...manifest, effects: [] };

        for (const { type, args, id: plannedId } of planEffects(config, projectDir)) {
          const effect = registry.get(type);
          if (!effect) {
            throw new Error(`Provider emitted an unregistered effect type "${type}"`);
          }
          const id = plannedId ?? stableEffectId(type, args);
          const occurrence = cursor.get(type) ?? 0;
          cursor.set(type, occurrence + 1);

          let previous = priorById.get(id);
          if (previous === undefined) {
            previous = priorByType.get(type)?.[occurrence];
          }
          const applyArgs = previous === undefined ? args : { ...args, previous };

          const beforeState = effect.apply(applyArgs);
          manifest = recordEffect(manifest, { type, id, beforeState });
          // F-001: after each successful apply, flush incomplete journal so
          // crash/SIGKILL leaves disk ownership matching journaled effects.
          // Rollback-only recovery is rejected — durability is the primary
          // mechanism (optional in-process reverse remains a consumer choice).
          manifest = {
            ...manifest,
            transaction: {
              ...manifest.transaction,
              state: 'incomplete',
              journalMode: 'per-effect',
              appliedCount: readEffects(manifest).length,
            },
          };
          writeManifest(projectDir, manifest, manifestDir);
        }

        manifest = {
          ...manifest,
          transaction: {
            ...manifest.transaction,
            state: 'complete',
            journalMode: 'per-effect',
            appliedCount: readEffects(manifest).length,
            completedAt: new Date().toISOString(),
          },
        };
        writeManifest(projectDir, manifest, manifestDir);
        return manifest;
      } finally {
        locks.release();
      }
    },

    uninstall({ projectDir }) {
      assertNoIncompleteTransaction(projectDir, manifestDir);
      const locks = acquireInstallLocks({
        projectDir,
        extra: resolveExtraIdentities(projectDir, {}),
        lockRoot,
      });
      try {
        const manifest = readManifest(projectDir, manifestDir);
        if (manifest == null) return;

        replayReverse(manifest, { basePath: projectDir, manifestDir }, registry);
        removeManifest(projectDir, manifestDir);
      } finally {
        locks.release();
      }
    },
  };
};

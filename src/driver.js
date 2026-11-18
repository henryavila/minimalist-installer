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

        const planned = planEffects(config, projectDir);
        const plannedTextIds = new Set(planned.filter(({ type }) => type === 'textBlock')
          .map(({ args }) => stableEffectId('textBlock', args)));
        const usedTextStates = new Set();
        const textMarkers = new Set();
        for (const { type, args } of planned) {
          if (type !== 'textBlock') continue;
          const key = JSON.stringify([args.path,
            args.markers?.begin ?? `<!-- BEGIN ${args.id} -->`,
            args.markers?.end ?? `<!-- END ${args.id} -->`]);
          if (textMarkers.has(key)) throw new Error(`textBlock marker collision at "${args.path}"`);
          textMarkers.add(key);
        }

        for (const { type, args, id: plannedId } of planned) {
          const effect = registry.get(type);
          if (!effect) {
            throw new Error(`Provider emitted an unregistered effect type "${type}"`);
          }
          const id = plannedId ?? stableEffectId(type, args);
          const occurrence = cursor.get(type) ?? 0;
          cursor.set(type, occurrence + 1);

          let previous = priorById.get(id);
          if (type === 'textBlock') {
            // Text blocks transfer ownership by explicit identity, never by
            // occurrence. A renamed id may claim only an unreserved predecessor
            // in the same file; unchanged markers disambiguate multiple blocks.
            const states = priorByType.get(type) ?? [];
            previous = states.find((state) => state.path === args.path && state.id === args.id);
            if (!previous) {
              const candidates = states.filter((state) => state.path === args.path
                && !usedTextStates.has(state)
                && !plannedTextIds.has(stableEffectId(type, state)));
              const begin = args.markers?.begin ?? `<!-- BEGIN ${args.id} -->`;
              const end = args.markers?.end ?? `<!-- END ${args.id} -->`;
              const matching = candidates.filter((state) => state.begin === begin && state.end === end);
              if (matching.length > 1 || (!matching.length && candidates.length > 1)) {
                throw new Error(`Ambiguous textBlock identity migration at "${args.path}"`);
              }
              previous = matching[0] ?? candidates[0];
            }
            const begin = args.markers?.begin ?? `<!-- BEGIN ${args.id} -->`;
            const end = args.markers?.end ?? `<!-- END ${args.id} -->`;
            if (states.some((state) => state !== previous && state.path === args.path
              && state.begin === begin && state.end === end)) {
              throw new Error(`textBlock marker collision at "${args.path}"`);
            }
            if (previous) {
              usedTextStates.add(previous);
              // The effect still rejects arbitrary mismatched direct callers.
              previous = { ...previous, id: args.id };
            }
          } else if (previous === undefined) {
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

import {
  readManifest,
  writeManifest,
  removeManifest,
  MANIFEST_DIR,
} from './manifest.js';
import { readEffects, recordEffect, replayReverse } from './kernel/journal.js';

// The Driver is identical for every consumer. It runs the configured providers to
// emit effects, applies each effect, journals its before-state on the manifest,
// and persists the manifest. Uninstall replays the journal in reverse (the
// structural uninstall) and removes the manifest — no consumer writes revert logic.
//
//   createDriver({ registry, providers, manifestDir }) -> { install, uninstall }
//
// MVP scope: greenfield install + structural uninstall. Re-install/update
// semantics (read prior manifest, 3-hash reconcile, orphan removal) are the next
// slice and are intentionally not handled here yet.
export const createDriver = ({ registry, providers, manifestDir = MANIFEST_DIR }) => {
  const planEffects = (config, projectDir) => {
    const planCtx = { basePath: projectDir, manifestDir };
    return providers.flatMap((provider) => provider.plan(config, planCtx));
  };

  return {
    install(config, { projectDir }) {
      // On re-install, each effect's prior before-state is threaded into its
      // apply as `previous` so it can reconcile against what it last installed
      // (the file effect uses it for 3-hash update + orphan removal). Prior
      // entries are matched to new ones by (type, occurrence order); providers
      // are pure planners so that order is stable across installs.
      const priorByType = new Map();
      const prior = readManifest(projectDir, manifestDir);
      if (prior) {
        for (const { type, beforeState } of readEffects(prior)) {
          if (!priorByType.has(type)) priorByType.set(type, []);
          priorByType.get(type).push(beforeState);
        }
      }

      const cursor = new Map();
      let manifest = {};

      for (const { type, args } of planEffects(config, projectDir)) {
        const effect = registry.get(type);
        if (!effect) {
          throw new Error(`Provider emitted an unregistered effect type "${type}"`);
        }
        const occurrence = cursor.get(type) ?? 0;
        cursor.set(type, occurrence + 1);
        const previous = priorByType.get(type)?.[occurrence];
        const applyArgs = previous === undefined ? args : { ...args, previous };

        const beforeState = effect.apply(applyArgs);
        manifest = recordEffect(manifest, { type, beforeState });
      }

      writeManifest(projectDir, manifest, manifestDir);
      return manifest;
    },

    uninstall({ projectDir }) {
      const manifest = readManifest(projectDir, manifestDir);
      if (manifest == null) return;

      // One shared revert ctx for the whole journal; effects read install-root
      // context here and their own before-state from the recorded entry.
      replayReverse(manifest, { basePath: projectDir, manifestDir }, registry);
      removeManifest(projectDir, manifestDir);
    },
  };
};

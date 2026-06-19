import {
  readManifest,
  writeManifest,
  removeManifest,
  MANIFEST_DIR,
} from './manifest.js';
import { recordEffect, replayReverse } from './kernel/journal.js';

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
      let manifest = {};

      for (const { type, args } of planEffects(config, projectDir)) {
        const effect = registry.get(type);
        if (!effect) {
          throw new Error(`Provider emitted an unregistered effect type "${type}"`);
        }
        const beforeState = effect.apply(args);
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

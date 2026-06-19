import { createEffectRegistry } from './kernel/effect.js';
import { createReconcileFileSetEffect } from './kernel/reconciler.js';
import { createJsonMergeEffect } from './kernel/effects/json-merge.js';
import { createRefcountEffect } from './kernel/effects/refcount.js';
import { createLegacyPruneEffect } from './kernel/effects/legacy-prune.js';
import { createDriver } from './driver.js';

// The two-tier config boundary (D2), as one factory:
//
//   - DECLARATIVE tier  → `config` (plain data the providers consume; the engine
//     owns only `config.manifestDir` and otherwise passes it through untouched).
//   - CODE tier         → `providers` + `effects` (the runtime-layer escape
//     hatch: a consumer registers a new effect type by adding it to `effects`,
//     and a new planner by adding it to `providers` — no kernel changes).
//
// The 4 built-in effect types are always registered (P4); consumer `effects`
// extend the catalog. Returns an installer bound to `config`.
const builtinEffects = () => [
  createReconcileFileSetEffect(),
  createJsonMergeEffect(),
  createRefcountEffect(),
  createLegacyPruneEffect(),
];

export const defineInstaller = ({ effects = [], providers = [], config = {} } = {}) => {
  if (!Array.isArray(providers) || providers.length === 0) {
    throw new Error('defineInstaller requires at least one provider');
  }

  const registry = createEffectRegistry();
  for (const effect of [...builtinEffects(), ...effects]) {
    registry.register(effect);
  }

  const driver = createDriver({ registry, providers, manifestDir: config.manifestDir });

  return {
    install: ({ projectDir }) => driver.install(config, { projectDir }),
    uninstall: ({ projectDir }) => driver.uninstall({ projectDir }),
    registry,
  };
};

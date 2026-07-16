// @henryavila/minimalist-installer — public API surface.
//
// The reversible, userland file+config installer engine. Consumers compose
// these primitives; they never write uninstall logic — reversibility is a
// property of each effect.

// Effect kernel — the closed apply()/revert(beforeState) contract + registry.
export { createEffectRegistry } from './kernel/effect.js';

// Journal — records each applied non-file effect's before-state on a manifest
// and replays revert in reverse order (the structural uninstall).
export { readEffects, recordEffect, replayReverse } from './kernel/journal.js';

// Manifest — the hash-aware ledger the journal extends (consumer-overridable
// manifest directory).
export {
  readManifest,
  writeManifest,
  removeManifest,
  MANIFEST_DIR,
  MANIFEST_FILE,
} from './manifest.js';

// Provider contract + reference provider — pure planners that emit effects.
export { createFileSetProvider } from './provider.js';

// Driver — the consumer-agnostic install/uninstall orchestrator over the kernel.
export { createDriver } from './driver.js';

// defineInstaller — two-tier config factory (declarative `config` + code-tier
// `effects`/`providers`); auto-registers the 4 built-in effect types.
export { defineInstaller } from './define-installer.js';

// Reconciler — declarative 3-hash file-set reconciliation (the file domain).
export { classifyFile, createReconcileFileSetEffect } from './kernel/reconciler.js';

// Built-in non-file effects (each reversible by before-state, not by snapshot).
export { createJsonMergeEffect } from './kernel/effects/json-merge.js';
export { createRefcountEffect } from './kernel/effects/refcount.js';
export { createLegacyPruneEffect } from './kernel/effects/legacy-prune.js';

// Content hashing used across the engine.
export { hashContent } from './hash.js';

// No-follow path authority + typed path safety errors.
export {
  PathSafetyError,
  ERR_UNSAFE_PATH_RACE,
  ERR_PATH_ESCAPE,
  ERR_GREENFIELD_CONFLICT,
  writeFileNoFollow,
  readFileNoFollow,
  unlinkNoFollow,
  existsNoFollow,
  atomicWriteJsonNoFollow,
  openParentNoFollow,
  assertLexicalWithinBase,
} from './path-safety.js';

// Canonical resource locks + recovery / inspect.
export {
  acquireLocks,
  acquireInstallLocks,
  resourceIdentity,
  lockFileName,
  DEFAULT_LOCK_ROOT,
} from './lock.js';
export {
  inspectTransaction,
  describeRecovery,
  assertNoIncompleteTransaction,
  TX_STATE_COMPLETE,
  TX_STATE_INCOMPLETE,
  TX_STATE_ABSENT,
} from './recovery.js';
export { stableEffectId, JOURNAL_VERSION } from './kernel/journal.js';

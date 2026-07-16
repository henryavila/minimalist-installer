export const JOURNAL_VERSION = 2;

export const readEffects = (manifest) => {
  if (manifest == null || !Object.hasOwn(manifest, 'effects')) {
    return [];
  }

  return manifest.effects;
};

/**
 * Record an applied effect. Prefer stable `id` (T-004); fall back to type-only
 * for callers that have not migrated yet.
 */
export const recordEffect = (manifest, { type, id, beforeState }) => ({
  ...manifest,
  journalVersion: JOURNAL_VERSION,
  effects: [
    ...readEffects(manifest),
    {
      type,
      ...(id != null ? { id } : {}),
      beforeState,
    },
  ],
});

export const replayReverse = (manifest, ctx, registry) => {
  for (const entry of [...readEffects(manifest)].reverse()) {
    const { type, beforeState } = entry;
    const effectType = registry.get(type);

    if (!effectType) {
      // Unknown future effects: diagnose but do not abort the entire revert
      // for other known effects. Surface via console-less error aggregation.
      const err = new Error(`Cannot revert unknown effect type "${type}"`);
      err.code = 'UNKNOWN_EFFECT';
      err.effect = entry;
      // Preserve prior behavior for strict registries: throw.
      // Consumers that want soft-skip can catch UNKNOWN_EFFECT.
      throw err;
    }

    effectType.revert(ctx, beforeState);
  }
};

/**
 * Build a stable effect id from type + discriminant args.
 * reconcileFileSet is singleton per install; jsonMerge keys on path; etc.
 */
export function stableEffectId(type, args = {}) {
  switch (type) {
    case 'reconcileFileSet':
      return 'reconcileFileSet';
    case 'jsonMerge':
      return `jsonMerge:${args.path ?? ''}`;
    case 'refcount':
      return `refcount:${args.ownersDir ?? ''}:${args.ownerId ?? ''}`;
    case 'legacyPrune':
      return `legacyPrune:${(args.legacyNamespaceDirs ?? []).join(',')}`;
    case 'stageRuntimeArtifacts':
      return `stageRuntimeArtifacts:${(args.items ?? []).map((i) => i.path).join(',')}`;
    default:
      return `${type}:${JSON.stringify(args)}`;
  }
}

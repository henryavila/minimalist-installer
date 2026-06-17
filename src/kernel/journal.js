export const readEffects = (manifest) => {
  if (manifest == null || !Object.hasOwn(manifest, 'effects')) {
    return [];
  }

  return manifest.effects;
};

export const recordEffect = (manifest, { type, beforeState }) => ({
  ...manifest,
  effects: [
    ...readEffects(manifest),
    { type, beforeState },
  ],
});

export const replayReverse = (manifest, ctx, registry) => {
  for (const { type, beforeState } of [...readEffects(manifest)].reverse()) {
    const effectType = registry.get(type);

    if (!effectType) {
      throw new Error(`Cannot revert unknown effect type "${type}"`);
    }

    effectType.revert(ctx, beforeState);
  }
};

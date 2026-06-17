const validateEffectType = (effectType, registeredTypes) => {
  const type = effectType?.type;

  if (typeof type !== 'string' || type.trim() === '') {
    throw new Error('Effect type must be a non-empty string');
  }

  if (registeredTypes.has(type)) {
    throw new Error(`Effect type "${type}" is already registered`);
  }

  if (typeof effectType.apply !== 'function') {
    throw new Error(`Effect type "${type}" must define an apply function`);
  }

  if (typeof effectType.revert !== 'function') {
    throw new Error(`Effect type "${type}" must define a revert function`);
  }
};

export const createEffectRegistry = () => {
  const effectTypes = new Map();

  return {
    register(effectType) {
      validateEffectType(effectType, effectTypes);
      effectTypes.set(effectType.type, effectType);
    },

    get(type) {
      return effectTypes.get(type);
    },

    has(type) {
      return effectTypes.has(type);
    },

    list() {
      return Array.from(effectTypes.keys());
    },
  };
};

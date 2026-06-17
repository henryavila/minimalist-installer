import { describe, it } from 'node:test';
import { strict as assert } from 'node:assert';
import { createEffectRegistry } from '../../src/kernel/effect.js';

const validEffect = (type = 'setKey') => ({
  type,
  apply() {
    return undefined;
  },
  revert() {},
});

describe('effect registry', () => {
  it('rejects duplicate effect type ids', () => {
    const registry = createEffectRegistry();

    registry.register(validEffect('duplicate'));

    assert.throws(() => registry.register(validEffect('duplicate')), /already registered/);
  });

  it('rejects invalid effect contracts', () => {
    const registry = createEffectRegistry();

    assert.throws(() => registry.register({ apply() {}, revert() {} }), /type/);
    assert.throws(() => registry.register({ type: '', apply() {}, revert() {} }), /type/);
    assert.throws(() => registry.register({ type: 'missingApply', revert() {} }), /apply/);
    assert.throws(() => registry.register({ type: 'missingRevert', apply() {} }), /revert/);
    assert.throws(() => registry.register({ type: 'invalidRevert', apply() {}, revert: true }), /revert/);
  });

  it('round-trips an effect back to the baseline state', () => {
    const baseline = { existing: 'value', target: 'before' };
    const ctx = {
      target: { ...baseline },
      key: 'target',
      value: 'after',
    };
    const registry = createEffectRegistry();

    registry.register({
      type: 'setKey',
      apply({ target, key, value }) {
        const beforeState = { hadKey: Object.hasOwn(target, key), value: target[key] };
        target[key] = value;
        return beforeState;
      },
      revert({ target, key }, beforeState) {
        if (beforeState.hadKey) {
          target[key] = beforeState.value;
        } else {
          delete target[key];
        }
      },
    });

    const effectType = registry.get('setKey');
    const beforeState = effectType.apply(ctx);
    assert.deepStrictEqual(ctx.target, { existing: 'value', target: 'after' });

    effectType.revert(ctx, beforeState);

    assert.deepStrictEqual(ctx.target, baseline);
  });

  it('exposes registered effect ids in registration order', () => {
    const registry = createEffectRegistry();

    registry.register(validEffect('first'));
    registry.register(validEffect('second'));

    assert.equal(registry.has('first'), true);
    assert.equal(registry.has('missing'), false);
    assert.deepStrictEqual(registry.list(), ['first', 'second']);
  });
});

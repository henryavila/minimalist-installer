// Provider contract.
//
// A provider is a PURE PLANNER. It implements:
//
//   plan(config, planCtx) -> Array<{ type, args }>
//
// where `type` is a registered effect type and `args` is the input passed to that
// effect's apply(). `planCtx` carries install-root context the Driver owns:
//   { basePath, manifestDir }.
//
// A provider NEVER executes effects and NEVER writes revert logic. The Driver
// applies the emitted effects and the journal reverts them structurally, so
// reversibility is a property of the effects, not of any provider.

// Reference provider (generic, not skills-specific): maps a desired file set to a
// single reconcileFileSet effect. `config.files` is `[{ path, content }]`.
export const createFileSetProvider = () => ({
  plan(config, { basePath }) {
    const files = config.files ?? [];
    return [{ type: 'reconcileFileSet', args: { basePath, desired: files } }];
  },
});

# Provider + Driver API (F2)

Status: **MVP landed** (install + structural uninstall over the file domain).
Tracked here in the package repo (F2 is package work; the atomic-skills plan
retains only F3, the consumption). Decisions below are the package's contract.

## The two roles

```
config ──▶ Provider.plan(config, planCtx) ──▶ [{ type, args }] ──▶ Driver ──▶ effects + journal
```

### Provider — pure planner

```js
provider.plan(config, planCtx) -> Array<{ type, args }>
```

- `type` is a **registered effect type**; `args` is the input to that effect's
  `apply()`.
- `planCtx = { basePath, manifestDir }` — install-root context the Driver owns and
  injects, so a provider never has to be told where the install root is out of band.
- A provider **never executes effects and never writes revert logic.**
  Reversibility is a property of the effects + journal, not of any provider.
  This is what keeps uninstall structural under a generic kernel (D4/D6).

Reference provider shipped: `createFileSetProvider()` — maps `config.files`
(`[{ path, content }]`) to a single `reconcileFileSet` effect. Generic, not
skills-specific (DECIDIDO #1: the SkillsProvider lives in the consumer).

### Driver — consumer-agnostic orchestrator

```js
createDriver({ registry, providers, manifestDir }) -> { install, uninstall }

driver.install(config, { projectDir })   // apply each emitted effect, journal before-state, persist manifest
driver.uninstall({ projectDir })         // replayReverse the journal + remove the manifest
```

- **install**: for each `{ type, args }` the providers emit, look up the effect in
  the registry, `apply(args)` → `beforeState`, `recordEffect(manifest, …)`, then
  `writeManifest`. An unregistered `type` throws (fail-fast, not silent skip).
- **uninstall**: `replayReverse(manifest, ctx, registry)` reverts every effect in
  reverse record order; then `removeManifest` reclaims the manifest dir so the
  round-trip is byte-for-byte. A never-installed root is a no-op.
- **Shared revert ctx**: the journal passes ONE `ctx = { basePath, manifestDir }`
  to every effect's `revert(ctx, beforeState)`. Effects read install-root context
  from `ctx` and everything else from their own recorded `beforeState`.

## MVP scope / not yet

- **In:** greenfield install, structural uninstall, no-clobber on user-modified
  files (from the reconciler), fail-fast on unknown effect type.
- **Out (next slice):** re-install/update semantics — reading the prior manifest,
  3-hash reconcile (`classifyFile` already exists), orphan removal; the config
  two-tier schema (T-F2-3); the runtime-layer registration example (T-F2-4,
  beyond the registry that already exists).

## Verification

`node --test test/driver/install-uninstall.test.js` (5 cases) +
`npm test` (full suite). The round-trip case asserts the install root returns to
empty after uninstall — the parity contract, now proven at the Driver level.

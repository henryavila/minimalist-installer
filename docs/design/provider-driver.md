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

## Scope

- **In:** greenfield install; structural uninstall; **re-install/update**
  (non-interactive 3-hash reconcile + orphan removal, below); no-clobber on
  user-modified files; fail-fast on an unregistered effect type.
- **Out (next slice):** the declarative **two-tier config** schema (T-F2-3); the
  **runtime-layer** registration example (T-F2-4, beyond the registry that
  already exists).

## Update / re-install policy (ported from the legacy installer, non-interactive)

On re-install the Driver threads each effect's prior before-state into its apply
as `previous` (matched by type + occurrence order — providers are pure planners,
so the order is stable). The file effect then applies the legacy `--yes` policy:

- **disk == last-installed** → overwrite with the new content.
- **disk != last-installed** (user edited) → **keep the user's file** (no clobber).
- **dropped from desired + unmodified** → remove (orphan).
- **dropped from desired + user-modified** → keep, and stop tracking it.

### Data-safety: user-edited files survive uninstall (resolved 2026-06-19)

A user-modified file is preserved on uninstall in BOTH cases, symmetrically:

- a kept **orphan** (dropped from desired) → untracked → survives uninstall.
- a kept file **still in desired** → tracked with its *original* installed hash,
  so it reads as "modified" forever; `revert` only deletes when disk == tracked
  hash, so the user's edits survive uninstall too.

This intentionally diverges from the legacy installer (which recorded the user's
content hash at `install.js:890` and thus reclaimed the file on uninstall). The
round-trip parity gate never exercises an edit-then-uninstall on current paths,
so the divergence is safe, and P3 ("no proof-less deletion of user content") wins.

## Verification

`node --test test/driver/install-uninstall.test.js` (5) +
`test/driver/update.test.js` (3) + `test/kernel/reconciler-update.test.js` (5),
plus `npm test` (full suite, **51**). The round-trip cases assert the install
root returns to empty after uninstall — including after an update — while a
user-edited file is proven to survive uninstall, at both the effect and Driver
level.

# @henryavila/minimalist-installer

A reversible, **userland** installer engine for CLI tools and AI skills.

It reconciles a target directory tree (a project repo or `$HOME`) to a
declaratively-defined set of **rendered files + structured config mutations**,
and every mutation it makes knows how to undo itself. A clean uninstall is a
property of the engine, not code each consumer writes.

It is *minimalist* by design: it writes files and merges config in userland and
nothing more — no OS integration, no package management, no template engine, no
multi-machine transactions. Small surface, strong guarantee (byte-for-byte
round-trip).

## What it is

- A **declarative reconciler**: you describe the desired set of generated
  artifacts (rendered templated files + config mutations such as a `settings.json`
  merge, a shared-artifact refcount, a legacy-path cleanup); the engine makes the
  target match it, idempotently.
- **Reversible by construction**: each mutation is a typed *effect* with an
  `apply()` and a `revert(beforeState)`. Uninstall replays the journal in reverse
  and reconciles the file set to empty — returning the target byte-for-byte to its
  pre-install state.
- **Provider-driven**: *what* to install is supplied by a consumer (a "provider").
  CLI tools and AI skills are the motivating consumers; the engine knows nothing
  about either.

## What it is NOT

- **Not** a package manager — no dependency or version resolution between consumers.
- **Not** an OS integrator — no Windows registry, no services/daemons, no system
  PATH, no privileged operations. It only writes files and merges config files in
  userland (`$HOME`, project dirs, `~/.config`-style locations).
- **Not** a template engine — rendering is the provider's job; the engine's core is
  reconcile + reverse.
- **Not** a transactional multi-machine rollback system — the scope is one local,
  reversible installation.

## Install

```sh
npm install @henryavila/minimalist-installer
```

## Status

**v0.1.0.** The engine core, extracted from `@henryavila/atomic-skills` (its first
consumer) and now published to npm.

Delivered: the effect kernel + journal + 3-hash file reconciler + the three
built-in non-file effects (`json-merge`, `refcount`, `legacy-prune`), each with a
round-trip / adversarial test suite; the **Provider contract** + a reference
`createFileSetProvider()`; the **Driver** (`install` / `uninstall` / **update**,
structural round-trip, no-clobber on user edits); **`defineInstaller`** (the
two-tier config factory); and a **runtime-layer worked example**
([`examples/symlink-runtime-layer.js`](examples/symlink-runtime-layer.js)) — see
[`docs/design/provider-driver.md`](docs/design/provider-driver.md).

There is no CLI — this is a **library**; the consumer owns its own CLI (lib-only
by design).

## API

```js
import {
  defineInstaller,
  createDriver,
  createFileSetProvider,
  createEffectRegistry,
  createReconcileFileSetEffect,
  createJsonMergeEffect,
  createRefcountEffect,
  createLegacyPruneEffect,
  recordEffect,
  replayReverse,
  readManifest,
  writeManifest,
} from '@henryavila/minimalist-installer';
```

- **`defineInstaller({ config, providers, effects })`** — two-tier config factory:
  declarative `config` (data; engine owns `manifestDir`, rest is pass-through to
  providers) + code-tier `providers`/`effects` (auto-registers the 4 built-ins).
  Returns `{ install, uninstall, registry }` bound to `config`.
- **`createDriver({ registry, providers, manifestDir })`** — the lower-level
  consumer-agnostic orchestrator: `install(config, { projectDir })` applies the
  providers' effects and journals them; `uninstall({ projectDir })` replays the
  journal in reverse and removes the manifest (structural, byte-for-byte round-trip).
- **`createFileSetProvider()`** — reference provider: maps `config.files`
  (`[{ path, content }]`) to a `reconcileFileSet` effect. Generic, not skills-specific.

- **`createEffectRegistry()`** — register/look up effect types (`{ type, apply, revert }`).
- **`createReconcileFileSetEffect()`** — declarative 3-hash file reconciliation
  (`apply({ basePath, desired })` → before-state; `revert` removes only
  unmodified installed files and prunes empty parents).
- **`createJsonMergeEffect()`** — additive JSON merge; revert subtracts only the
  inserted delta (never a snapshot), preserving third-party content; deletes a
  file it created once emptied.
- **`createRefcountEffect()`** — per-owner marker directory validated against each
  owner's manifest; reclaims a shared artifact only when the last owner leaves.
  Crash-safe (no decrement step).
- **`createLegacyPruneEffect()`** — deletes only files carrying the consumer's
  frontmatter signature (no proof of ownership ⇒ never deletes); reversible (restores
  pruned files on revert).
- **Journal** (`recordEffect`/`readEffects`/`replayReverse`) + **manifest**
  (`readManifest`/`writeManifest`, consumer-overridable directory).

## Test

```sh
npm test    # node --test "test/**/*.test.js"  (requires Node >= 21 for glob expansion)
```

## License

MIT

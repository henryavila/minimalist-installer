# @henryavila/tooling-installer

A reversible, **userland** installer engine for CLI tools and AI skills.

It reconciles a target directory tree (a project repo or `$HOME`) to a
declaratively-defined set of **rendered files + structured config mutations**,
and every mutation it makes knows how to undo itself. A clean uninstall is a
property of the engine, not code each consumer writes.

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

## Status

**v0.1.0 — early.** This is the engine core extracted from `@henryavila/atomic-skills`
(its first consumer), which will depend on it via a local link until the API
stabilizes, then via the npm release.

Delivered so far: the effect kernel + journal + 3-hash file reconciler + the three
built-in non-file effects (`json-merge`, `refcount`, `legacy-prune`), each with a
round-trip / adversarial test suite; and the **Provider contract** + a reference
`createFileSetProvider()` + the **Driver** (`install` / `uninstall`, structural
round-trip) — see [`docs/design/provider-driver.md`](docs/design/provider-driver.md).

Not yet here (next): re-install/**update** semantics in the Driver (3-hash
reconcile + orphan removal), the declarative **two-tier config** schema, and the
runtime-layer registration example. There is no CLI — this is a **library**; the
consumer owns its own CLI (lib-only by design).

## API

```js
import {
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
} from '@henryavila/tooling-installer';
```

- **`createDriver({ registry, providers, manifestDir })`** — the consumer-agnostic
  orchestrator: `install(config, { projectDir })` applies the providers' effects and
  journals them; `uninstall({ projectDir })` replays the journal in reverse and
  removes the manifest (structural, byte-for-byte round-trip).
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
npm test    # node --test "test/**/*.test.js"
```

## License

MIT

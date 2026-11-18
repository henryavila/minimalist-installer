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

Delivered: the effect kernel + journal + 3-hash file reconciler + the four
built-in non-file effects (`json-merge`, `text-block`, `refcount`, `legacy-prune`), each with a
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
  createTextBlockEffect,
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
  providers) + code-tier `providers`/`effects` (auto-registers the 5 built-ins).
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
- **`createTextBlockEffect()`** — managed delimited text block with exact byte
  restoration and plan-wide validation. See the nine-point contract below.
- **`createRefcountEffect()`** — per-owner marker directory validated against each
  owner's manifest; reclaims a shared artifact only when the last owner leaves.
  Crash-safe (no decrement step).
- **`createLegacyPruneEffect()`** — deletes only files carrying the consumer's
  frontmatter signature (no proof of ownership ⇒ never deletes); reversible (restores
  pruned files on revert).
- **Journal** (`recordEffect`/`readEffects`/`replayReverse`) + **manifest**
  (`readManifest`/`writeManifest`, consumer-overridable directory).

## textBlock contract

The public arguments remain `{ basePath, path, id, content, markers?, previous? }`.
Content is a string with trailing line endings stripped; default markers are
`<!-- BEGIN ${id} -->` and `<!-- END ${id} -->`. The generated segment uses LF.

1. Markers match whole lines exactly, excluding only LF or CRLF; prefixes and
   inline occurrences are user text.
2. With no block, append and record the exact inserted bytes, separators included;
   revert removes those bytes. A file created by the installer is deleted once empty.
3. On the first install of an effect id (which can be a later installer run), one
   well-formed existing block may be adopted. Its exact original bytes are saved
   and restored on revert. Each journaled block owns its end-marker terminator;
   a terminator added to an adopted EOF block is removed on revert.
4. Updates with the same id and markers replace the owned content in place and
   preserve the original before-state.
5. Changing id or markers removes the old substitution and installs fresh. If any
   new marker already appears as a whole line after removal, refuse. There is no
   migration onto an existing block or transfer of ownership between ids.
6. Refuse duplicate, unbalanced or nested markers; empty or equal markers; planned
   effects sharing an id or markers on one file; and content containing any planned
   or journaled marker line on that file. Errors name the file and reason, regardless
   of planned array order.
7. Validate the whole plan before mutation. A refusal preserves target files and
   manifest byte-for-byte.
8. Revert leaves hand-edited content, missing blocks and broken markers untouched,
   silently returning as defined by the original effect contract. Updates also
   preserve a hand-edited block. Broken markers make later installs refuse, even
   when dropping that effect or updating an unrelated effect.
9. Journal entries from `0e12991` remain readable and reversible.

Known limit: collision checks compare path strings. On case-insensitive filesystems,
paths such as `a.md` and `A.md` can refer to the same file without being detected
as a collision; use the same spelling for every effect targeting that file.

## Test

```sh
npm test    # node --test "test/**/*.test.js"  (requires Node >= 21 for glob expansion)
```

## License

MIT

## Python variation

The Python distribution lives in a separate public repository:
[henryavila/minimalist-installer-python](https://github.com/henryavila/minimalist-installer-python).

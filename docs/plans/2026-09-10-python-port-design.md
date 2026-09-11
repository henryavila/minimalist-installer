# Minimalist Installer Python Port — Design

**Date:** 2026-09-10  
**Status:** approved in conversation  
**First consumer:** `lacuna-signer` agent-skill installer

## Problem

`@henryavila/minimalist-installer` provides a robust, reversible userland
installer engine in JavaScript. Python projects that distribute agent skills
should not need Node, should not copy IDE paths, and should not invent their own
unsafe install/update/uninstall logic.

The Python implementation must preserve the complete capability envelope of
the JavaScript engine while allowing a Pythonic API and deliberate improvements.
It must remain generic: skill distribution is an official module and first
consumer, not a concern embedded in the kernel.

## Goals

- Ship a reusable Python package for reversible userland installations.
- Preserve all existing concepts: providers, effects, registry, driver,
  manifests, journal, three-hash reconciliation, JSON merge, refcount,
  legacy prune, update, uninstall, locking, recovery, and path safety.
- Improve crash safety with a write-ahead transaction journal.
- Provide an official skills module with automatic, extensible host detection.
- Provide a reusable TUI equivalent in interaction and visual hierarchy to the
  Atomic Skills installer.
- Keep consumers free of IDE names, paths, detection logic, and TUI code.
- Support Linux, macOS, and Windows from the first releasable version.
- Keep the core synchronous and standard-library-only.

## Non-goals

- Byte-for-byte API or manifest compatibility with the JavaScript package.
- Replacing the JavaScript package or making Atomic Skills invoke Python.
- OS package management, services, privileged installation, or global PATH
  mutation.
- Network package resolution or a multi-machine transaction coordinator.
- Storing consumer credentials such as `LACUNA_SIGNER_API_KEY`.

## Decisions

| ID | Decision |
|---|---|
| D1 | Keep JavaScript and Python in the same repository. Add Python under `python/`; do not initially move the existing Node files. |
| D2 | Share semantic specifications and conformance vectors under `spec/`; APIs and manifests may be language-native. |
| D3 | Divide Python into generic `core`, built-in `effects` and `providers`, official `skills`, and optional `tui`. |
| D4 | Require Python 3.11+. The core and skills planner use only the standard library. |
| D5 | Use Pythonic classes, dataclasses, enums, protocols, exceptions, and snake_case. Preserve a `define_installer()` convenience factory. |
| D6 | Effects use prepare/apply/revert. The engine persists prepared rollback state before the first mutation. |
| D7 | Keep the last committed `manifest.json` separate from an in-progress write-ahead transaction journal. |
| D8 | Every planned effect has a stable ID. Occurrence-order matching exists only for explicit legacy migration. |
| D9 | Acquire the complete, sorted resource lock set before preparation or mutation; no late lock acquisition. |
| D10 | Refuse symlink/reparse traversal and path escape. A platform without a safe mutation backend fails closed. |
| D11 | Built-in effects are `reconcile_file_set`, `json_merge`, `refcount`, and `legacy_prune`. |
| D12 | Host knowledge lives in the installer distribution as declarative adapters, extended through Python entry points. Consumers never hardcode host paths. |
| D13 | Detection returns confidence plus evidence. It preselects TUI choices but never authorizes writes. |
| D14 | Deduplicate identical physical destinations, including shared `~/.agents/skills` discovery. |
| D15 | TUI dependencies are optional through `minimalist-installer[tui]`; use Rich plus Questionary. |
| D16 | Non-interactive commands never prompt, support JSON output, and fail if no host is detected or selected. |
| D17 | User and project scope are first-class. Project scope resolves safely and refuses filesystem root, home-as-project, bare repositories, and unwritable targets. |
| D18 | The package reports `verified`, `layout-only`, or `external` host support. Installation support and real-agent workflow qualification remain distinct. |
| D19 | Publish the Python distribution as `minimalist-installer` on PyPI if the name remains available; the import is `minimalist_installer`. |
| D20 | `lacuna-signer` consumes the Python package through an optional `agents` extra and exposes `lacuna-signer skill ...`. |
| D21 | The skill bundle includes all referenced files and receives the absolute CLI path as a render variable; agents do not depend on virtualenv activation. |
| D22 | No API key is requested, rendered, journaled, or persisted by the installer. |

## Repository layout

```text
minimalist-installer/
├── src/                              # existing JavaScript implementation
├── test/                             # existing JavaScript tests
├── python/
│   ├── pyproject.toml
│   ├── src/minimalist_installer/
│   │   ├── __init__.py
│   │   ├── core/
│   │   │   ├── driver.py
│   │   │   ├── errors.py
│   │   │   ├── journal.py
│   │   │   ├── locks.py
│   │   │   ├── manifest.py
│   │   │   ├── models.py
│   │   │   ├── path_safety.py
│   │   │   ├── recovery.py
│   │   │   └── registry.py
│   │   ├── effects/
│   │   │   ├── file_set.py
│   │   │   ├── json_merge.py
│   │   │   ├── legacy_prune.py
│   │   │   └── refcount.py
│   │   ├── providers/
│   │   │   └── file_set.py
│   │   ├── skills/
│   │   │   ├── bundle.py
│   │   │   ├── detector.py
│   │   │   ├── distribution.py
│   │   │   ├── registry.py
│   │   │   └── hosts/*.toml
│   │   ├── tui/
│   │   │   ├── app.py
│   │   │   ├── messages.py
│   │   │   └── theme.py
│   │   └── cli.py
│   └── tests/
└── spec/
    ├── semantics/
    ├── schemas/
    └── conformance/
```

## Public Python API

```python
from minimalist_installer import define_installer

installer = define_installer(
    config=config,
    providers=[provider],
    effects=[custom_effect],
)

install_result = installer.install(base_path=path)
update_result = installer.update(base_path=path)
status = installer.status(base_path=path)
repair_result = installer.repair(base_path=path)
uninstall_result = installer.uninstall(base_path=path)
```

The library never calls `sys.exit()`. It returns immutable structured results
and raises typed exceptions carrying stable error codes. CLI and TUI translate
those results and errors into presentation and exit codes.

### Provider contract

```python
class Provider(Protocol):
    def plan(
        self,
        config: Mapping[str, object],
        context: PlanContext,
    ) -> Sequence[EffectPlan]: ...
```

Providers are pure planners. They do not execute effects and do not implement
uninstall logic.

### Effect contract

```python
class Effect(Protocol):
    type: str
    version: int

    def prepare(
        self,
        args: JsonObject,
        previous: JsonValue | None,
        context: EffectContext,
    ) -> PreparedEffect: ...

    def apply(
        self,
        prepared: PreparedEffect,
        checkpoint: CheckpointWriter,
    ) -> JsonValue: ...

    def revert(
        self,
        context: EffectContext,
        before_state: JsonValue,
    ) -> None: ...
```

`prepare()` must not mutate. It returns JSON-serializable rollback state,
resource identities, and an application payload. `apply()` may mutate only after
the engine has durably persisted that prepared state. Built-in effects support
idempotent checkpointed rollback. Custom effects must declare their recovery
capability; nonrecoverable effects are rejected by durable mode.

## Manifest and transaction model

```text
<manifest-dir>/
├── manifest.json                     # last committed installation
└── transactions/
    └── <transaction-id>/
        ├── journal.json               # write-ahead journal
        └── blobs/                     # content-addressed temporary backups
```

The committed manifest contains engine identity, schema version, installation
identity, consumer identity/version, stable effect records, and timestamps. It
never represents partially applied state.

The transaction journal records operation (`install`, `update`, `uninstall`, or
`repair`), phase, full planned effect IDs, prepared rollback state, per-effect
progress, and checkpoint progress. Every journal write uses same-directory
temporary creation, file flush, atomic replace, and directory flush where the
platform supports it.

Successful commit atomically replaces `manifest.json` and then removes the
transaction directory. A crash leaves the previous committed manifest plus a
self-describing transaction.

### Recovery rules

- Any incomplete transaction blocks unrelated mutation.
- `status()` and `inspect_recovery()` are always read-only.
- Trusted, supported transactions can be rolled back or resumed according to
  the effect recovery declaration.
- Default `repair()` rolls back; resume requires an explicit compatible plan.
- Unknown effects, unknown effect versions, corrupt JSON, missing blobs, or an
  untrusted legacy journal fail closed with a diagnostic report.
- Repair itself is checkpointed, so interruption during repair is recoverable.
- Completed transaction remnants may be garbage-collected only after proving
  the committed manifest contains the same transaction ID.

## Locking

Effects declare all external resource identities during planning/preparation.
The driver adds the installation root, canonicalizes and deduplicates the set,
sorts by raw identity, and acquires every lock before mutation.

The Python implementation uses held-open OS locks: `flock` on POSIX and native
Windows locking. Process termination releases locks without PID-based stealing.
Lock metadata is diagnostic only. Release happens in reverse order.

## Path and data safety

- Resolve a trusted absolute base without using a final path resolution that
  silently follows attacker-controlled descendants.
- Walk every path component with no-follow semantics.
- Refuse symlinks and Windows reparse points at intermediate and leaf paths.
- Enforce lexical containment before filesystem access.
- Use exclusive temporary files and atomic replacement in the same directory.
- Never delete a file without an ownership proof from its recorded content hash
  or reversible before-state.
- Preserve user-modified installed files on update and uninstall.
- Remove unmodified dropped files and prune only empty, installer-created
  parents bounded by the installation root.
- Never permit filesystem root or a home directory to become an implicit
  project target.

## Built-in effects

### `reconcile_file_set`

Uses desired, last-installed, and current-disk hashes:

- disk equals last installed: replace with new desired bytes;
- disk differs: preserve as user-owned conflict;
- dropped and unmodified: remove;
- dropped and modified: preserve and stop tracking;
- greenfield collision: refuse unless the caller explicitly adopts an identical
  desired file or uses a separately audited force policy.

Each file operation is checkpointed. Files replaced during an update are backed
up temporarily so a failed update can restore the prior installed version.

### `json_merge`

Adds only absent or equal structure, rejects scalar conflicts, deduplicates
array additions, and reverts only its owned delta. Third-party edits survive.

### `refcount`

Uses per-owner claims validated against owner manifests. Reclaim happens only
after proving that no valid owner remains. Orphans can be healed without
removing valid claims.

### `legacy_prune`

Removes only content proven to match configured legacy signatures. Removed
bytes are recorded for exact restoration. Unknown or edited content survives.

## Skills distribution

The skills module accepts either a Python `SkillDistribution` or a declarative
TOML descriptor:

```python
SkillDistribution(
    name="lacuna-signer",
    version="0.1.0",
    bundle=Path("agent_skill"),
    variables={"LACUNA_SIGNER_BIN": executable_path},
)
```

It validates the Agent Skills bundle, recursively inventories referenced files,
renders declared variables, resolves host destinations, deduplicates physical
paths, and emits a regular `reconcile_file_set` plan. It does not bypass the
generic engine.

## Host registry and automatic detection

Built-in host descriptors live under `minimalist_installer/skills/hosts/*.toml`.
The generic core contains no host IDs or paths. Third parties add adapters via
the `minimalist_installer.hosts` entry-point group.

Each adapter provides:

- stable ID and display name;
- support tier;
- detection signals;
- user/project destinations;
- discovery depth and naming rules;
- optional renderer for nonstandard formats;
- optional read-only qualification probe.

Detection combines executable presence, environment variables, configuration
directories, project markers, existing manifests, and compatible shared roots.
It returns confidence and evidence rather than a boolean. TUI preselection uses
a documented threshold; users still approve destinations.

Initial built-ins: Claude Code, Cursor, Codex, Gemini CLI, Grok Build, OpenCode,
and GitHub Copilot. Only hosts with real discovery/invocation receipts are
labeled `verified`; others remain `layout-only`.

Codex, Gemini, and Grok may share `~/.agents/skills`. Identical resolved targets
are written once and reported against every compatible host. Claude uses native
`.claude/skills`; new work does not use legacy `.claude/commands`. Standalone
Grok skills do not require plugin hooks.

## TUI and CLI

The optional TUI uses Rich for output/progress and Questionary for select,
multiselect, and confirmation. It mirrors the Atomic flow:

```text
intro/version
  -> user or project scope
  -> detected hosts with evidence
  -> host selection
  -> communication language
  -> planned changes and conflicts
  -> confirmation
  -> execution/progress
  -> per-host summary and next steps
```

The presentation layer consumes structured results; it never owns mutation
logic. It supports Portuguese and English, `NO_COLOR`, keyboard cancellation,
and terminals without Unicode. Cancellation before confirmation leaves no
transaction.

Generic CLI:

```text
minimalist-installer install <distribution.toml>
minimalist-installer update <distribution.toml>
minimalist-installer detect [--json]
minimalist-installer status <distribution.toml> [--json]
minimalist-installer repair <distribution.toml>
minimalist-installer uninstall <distribution.toml>
```

Non-interactive mode never prompts. `--yes` accepts a fully determined plan but
does not mean `--force`; conflicts remain preserved or cause a policy error.
No detected or explicit host is an error rather than an install-everywhere
fallback.

## Error and result contract

All library errors inherit `InstallerError` and include a stable code, human
message, operation, affected path/resource when safe, and structured details.
Primary codes include:

- `unsafe_path`, `greenfield_conflict`, `modified_content`;
- `lock_timeout`, `incomplete_transaction`, `corrupt_manifest`;
- `unknown_effect`, `unsupported_effect_version`, `recovery_blocked`;
- `no_host_detected`, `unsupported_host`, `invalid_distribution`;
- `non_interactive_input_required`.

Results expose planned/applied/preserved/conflict/stale/missing/reverted items,
transaction and installation IDs, selected hosts, resolved destinations, and
warnings. CLI JSON is versioned and stdout-only; human output uses stderr when
JSON mode is active.

## Packaging

Python metadata lives in `python/pyproject.toml` and uses Hatchling. The base
package contains core, effects, providers, skills, CLI, schemas, and host
descriptors with no runtime dependencies. Optional extras:

```toml
[project.optional-dependencies]
tui = ["rich", "questionary"]
dev = ["pytest", "pytest-cov", "hypothesis"]
```

The console script is `minimalist-installer`. Wheels and sdists are tested from
built artifacts, not only editable installs. PyPI name availability is checked
again immediately before publication.

## Shared specification and conformance

`spec/` defines behavior independently of JavaScript or Python syntax. JSON
fixtures cover hashing, classification, provider plans, effect IDs, merge
semantics, manifests, result normalization, and recovery decisions.

Both implementations run fixtures that apply to their supported schema. A
capability matrix records `equivalent`, `python-extension`, `node-extension`, or
`not-applicable`; parity is never inferred from similar names.

## Verification strategy

- Unit tests for every protocol, classifier, effect, serializer, and detector.
- Byte-for-byte install/uninstall round trips.
- Update round trips with user-modified files.
- Property tests for containment, merge/revert, identity ordering, and
  reconciliation invariants.
- Fault injection before and after every durable write and every effect
  checkpoint.
- Subprocess kill tests proving recovery after abrupt termination.
- Symlink/reparse adversarial tests with outside sentinel files.
- Concurrent install/update/uninstall tests over overlapping resources.
- Wheel/sdist black-box install tests.
- CI matrix for Linux, macOS, and Windows on Python 3.11, 3.12, and 3.13.
- Layout tests for every built-in host and real-session qualification receipts
  before a host is labeled verified.
- Existing Node test suite remains a required CI job.

## `lacuna-signer` integration

The `lacuna-signer` wheel moves its skill into a packaged bundle and declares an
optional `agents` extra. Production Arch installation does not need TUI
dependencies.

```text
pip install -e ".[agents]"
lacuna-signer skill install
lacuna-signer skill status
lacuna-signer skill uninstall
```

The command supplies `SkillDistribution`, product/version labels, translations,
and the absolute active console-script path. The generic installer detects
hosts, renders the bundle, manages manifests, and presents the TUI. It never
loads or stores Lacuna credentials.

## Delivery order

1. Establish Python package, types, error model, and shared spec skeleton.
2. Implement path safety, atomic persistence, locks, and manifests.
3. Implement registry, providers, write-ahead driver, and recovery.
4. Implement all built-in effects.
5. Add host registry, detection, skill bundle rendering, and scopes.
6. Add CLI and TUI.
7. Add cross-platform, fault-injection, artifact, and conformance gates.
8. Integrate `lacuna-signer` as the first external consumer.
9. Document migration and publish only after artifact-level verification.


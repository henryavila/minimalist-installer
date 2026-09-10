# Minimalist Installer Python Port Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deliver a production-grade Python implementation of the minimalist installer, including the generic reversible engine, automatic multi-agent skill distribution, equivalent TUI, cross-platform verification, and `lacuna-signer` integration.

**Architecture:** Keep the existing JavaScript package intact and add a Python distribution under `python/`. The Python engine uses providers and prepared/checkpointed effects over a write-ahead transaction journal; the skills layer resolves data-driven host adapters into ordinary file-set effects. Shared semantic fixtures under `spec/` prevent accidental divergence without requiring byte-compatible language APIs.

**Tech Stack:** Python 3.11+, Hatchling, pytest, Hypothesis, Rich, Questionary, GitHub Actions; existing Node 22 test suite remains mandatory.

**Design:** `docs/plans/2026-09-10-python-port-design.md`

---

### Task 1: Scaffold the Python distribution and public value types

**Files:**
- Create: `python/pyproject.toml`
- Create: `python/src/minimalist_installer/__init__.py`
- Create: `python/src/minimalist_installer/core/__init__.py`
- Create: `python/src/minimalist_installer/core/errors.py`
- Create: `python/src/minimalist_installer/core/models.py`
- Create: `python/tests/test_public_api.py`
- Modify: `.gitignore`

**Step 1: Write the failing public-API test**

Assert that immutable `EffectPlan`, `PlanContext`, `EffectContext`,
`PreparedEffect`, operation results, and typed `InstallerError` subclasses can
be imported from `minimalist_installer` and serialized without losing stable
error codes.

**Step 2: Verify RED**

Run: `python/.venv/bin/pytest python/tests/test_public_api.py -q`  
Expected: FAIL because the distribution and modules do not exist.

**Step 3: Add the package and minimal public types**

Use frozen dataclasses, string enums, `typing.Protocol`, `Path`, and JSON type
aliases. Configure Hatchling for a `src/` layout, Python `>=3.11`, base runtime
with no dependencies, `tui` and `dev` extras, and a `minimalist-installer`
entry point reserved for Task 10.

**Step 4: Verify GREEN**

Run: `python/.venv/bin/pytest python/tests/test_public_api.py -q`  
Expected: PASS.

**Step 5: Commit**

```bash
git add .gitignore python
git commit -m "feat(python): scaffold installer package"
```

### Task 2: Implement safe paths and atomic filesystem primitives

**Files:**
- Create: `python/src/minimalist_installer/core/path_safety.py`
- Create: `python/tests/core/test_path_safety.py`
- Create: `python/tests/core/test_atomic_io.py`

**Step 1: Write failing containment and sentinel tests**

Cover absolute paths, `..`, sibling-prefix escapes, symlinked intermediates,
symlink leaves, Windows reparse-point classification, exclusive temp creation,
atomic byte/JSON replacement, and empty-parent pruning bounded by the base.
Outside sentinel bytes must remain unchanged for every rejected operation.

**Step 2: Verify RED**

Run: `python/.venv/bin/pytest python/tests/core/test_path_safety.py python/tests/core/test_atomic_io.py -q`  
Expected: FAIL on missing safe-path API.

**Step 3: Implement the safe mutation backend**

Provide lexical containment, component-by-component no-follow inspection,
POSIX `dir_fd`/`O_NOFOLLOW`, Windows reparse checks, safe read/write/unlink,
same-directory atomic replacement, file flush, supported directory flush, and
bounded pruning. Fail closed through `UnsafePathError` when safe guarantees are
unavailable.

**Step 4: Verify GREEN and regressions**

Run: `python/.venv/bin/pytest python/tests/core/test_path_safety.py python/tests/core/test_atomic_io.py -q`  
Expected: PASS with sentinels intact.

**Step 5: Commit**

```bash
git add python/src/minimalist_installer/core/path_safety.py python/tests/core
git commit -m "feat(python): add fail-closed filesystem primitives"
```

### Task 3: Add resource locks and committed manifests

**Files:**
- Create: `python/src/minimalist_installer/core/locks.py`
- Create: `python/src/minimalist_installer/core/manifest.py`
- Create: `python/tests/core/test_locks.py`
- Create: `python/tests/core/test_manifest.py`
- Create: `spec/schemas/python-manifest-v1.schema.json`

**Step 1: Write failing lock and manifest tests**

Assert canonical resource identities, byte-order sorting, deduplication,
reverse release, contention timeout, release on subprocess exit, versioned
manifest validation, atomic writes, and refusal of foreign/corrupt schemas.

**Step 2: Verify RED**

Run: `python/.venv/bin/pytest python/tests/core/test_locks.py python/tests/core/test_manifest.py -q`  
Expected: FAIL on missing modules.

**Step 3: Implement OS locks and manifest repository**

Use held-open advisory locks (`fcntl` and `msvcrt` backends), diagnostic lock
metadata, a deterministic lock root, and a `ManifestRepository` that reads,
validates, atomically writes, and removes committed manifests without following
links.

**Step 4: Verify GREEN**

Run: `python/.venv/bin/pytest python/tests/core/test_locks.py python/tests/core/test_manifest.py -q`  
Expected: PASS.

**Step 5: Commit**

```bash
git add python/src/minimalist_installer/core/locks.py python/src/minimalist_installer/core/manifest.py python/tests/core spec/schemas
git commit -m "feat(python): add resource locks and manifests"
```

### Task 4: Build the effect registry, providers, WAL, and driver

**Files:**
- Create: `python/src/minimalist_installer/core/registry.py`
- Create: `python/src/minimalist_installer/core/journal.py`
- Create: `python/src/minimalist_installer/core/driver.py`
- Create: `python/src/minimalist_installer/providers/__init__.py`
- Create: `python/src/minimalist_installer/providers/file_set.py`
- Create: `python/tests/core/test_registry.py`
- Create: `python/tests/core/test_journal.py`
- Create: `python/tests/core/test_driver.py`
- Create: `spec/schemas/python-transaction-v1.schema.json`

**Step 1: Write failing protocol/transaction tests**

Test duplicate/invalid effects, pure provider planning, stable effect IDs,
prepare-before-apply ordering, journal flush before mutation, per-effect
checkpoints, stable prior matching, reverse replay, lock-before-prepare, unknown
effect failure, and preservation of the committed manifest until commit.

**Step 2: Verify RED**

Run: `python/.venv/bin/pytest python/tests/core/test_registry.py python/tests/core/test_journal.py python/tests/core/test_driver.py -q`  
Expected: FAIL.

**Step 3: Implement registry, WAL repository, and driver**

Implement `EffectRegistry`, `FileSetProvider`, `TransactionRepository`,
`CheckpointWriter`, `Driver`, `Installer`, and `define_installer()`. Plan all
effects and resources before locking; persist prepared rollback state before
apply; commit only after every checkpoint is durable.

**Step 4: Verify GREEN**

Run: `python/.venv/bin/pytest python/tests/core/test_registry.py python/tests/core/test_journal.py python/tests/core/test_driver.py -q`  
Expected: PASS.

**Step 5: Commit**

```bash
git add python/src/minimalist_installer/core python/src/minimalist_installer/providers python/tests/core spec/schemas
git commit -m "feat(python): add transactional provider driver"
```

### Task 5: Implement three-hash file-set reconciliation

**Files:**
- Create: `python/src/minimalist_installer/effects/__init__.py`
- Create: `python/src/minimalist_installer/effects/file_set.py`
- Create: `python/tests/effects/test_file_set.py`
- Create: `python/tests/effects/test_file_set_faults.py`
- Create: `spec/conformance/file-set.json`

**Step 1: Write failing classification and round-trip tests**

Cover greenfield, update of owned-unmodified bytes, preservation of user edits,
unmodified/modified orphans, missing files, adoption of identical bytes,
collision refusal, uninstall ownership proof, backup blobs, and injected failure
at every file checkpoint.

**Step 2: Verify RED**

Run: `python/.venv/bin/pytest python/tests/effects/test_file_set.py python/tests/effects/test_file_set_faults.py -q`  
Expected: FAIL.

**Step 3: Implement the prepared/checkpointed effect**

Hash bytes with SHA-256, normalize manifest paths to POSIX form, derive explicit
decisions, stage update backups content-addressably, mutate through safe
primitives, and implement idempotent revert.

**Step 4: Verify GREEN**

Run: `python/.venv/bin/pytest python/tests/effects/test_file_set.py python/tests/effects/test_file_set_faults.py -q`  
Expected: PASS.

**Step 5: Commit**

```bash
git add python/src/minimalist_installer/effects python/tests/effects spec/conformance/file-set.json
git commit -m "feat(python): reconcile file sets transactionally"
```

### Task 6: Implement JSON merge, refcount, and legacy prune

**Files:**
- Create: `python/src/minimalist_installer/effects/json_merge.py`
- Create: `python/src/minimalist_installer/effects/refcount.py`
- Create: `python/src/minimalist_installer/effects/legacy_prune.py`
- Create: `python/tests/effects/test_json_merge.py`
- Create: `python/tests/effects/test_refcount.py`
- Create: `python/tests/effects/test_legacy_prune.py`
- Create: `spec/conformance/json-merge.json`
- Create: `spec/conformance/refcount.json`
- Create: `spec/conformance/legacy-prune.json`

**Step 1: Port and extend failing adversarial cases**

Port every semantic case from the Node package and add prepared-state,
checkpoint, interrupted-revert, outside-sentinel, and corrupt-input cases.

**Step 2: Verify RED**

Run: `python/.venv/bin/pytest python/tests/effects/test_json_merge.py python/tests/effects/test_refcount.py python/tests/effects/test_legacy_prune.py -q`  
Expected: FAIL.

**Step 3: Implement all three effects**

Use the common prepared-effect/checkpoint contract and safe filesystem API.
JSON merge subtracts owned deltas only; refcount validates remaining owner
manifests; legacy prune requires configured ownership signatures and records
exact restoration bytes.

**Step 4: Verify GREEN**

Run: `python/.venv/bin/pytest python/tests/effects -q`  
Expected: PASS.

**Step 5: Commit**

```bash
git add python/src/minimalist_installer/effects python/tests/effects spec/conformance
git commit -m "feat(python): add reversible built-in effects"
```

### Task 7: Complete recovery, repair, and fault-injection guarantees

**Files:**
- Create: `python/src/minimalist_installer/core/recovery.py`
- Create: `python/tests/core/test_recovery.py`
- Create: `python/tests/integration/test_fault_matrix.py`
- Create: `python/tests/integration/crash_worker.py`

**Step 1: Write failing crash/recovery tests**

Spawn and terminate workers before/after every durable boundary for install,
update, uninstall, and repair. Assert read-only inspection, blocked unrelated
mutation, trusted rollback, checkpointed repair, missing-blob refusal, corrupt
journal refusal, unknown-effect refusal, and cleanup only after commit proof.

**Step 2: Verify RED**

Run: `python/.venv/bin/pytest python/tests/core/test_recovery.py python/tests/integration/test_fault_matrix.py -q`  
Expected: FAIL.

**Step 3: Implement recovery coordinator**

Add `inspect_recovery()`, `status()`, `repair()`, resumability declarations,
recovery reports, residual ledgers, and garbage collection of proved completed
transactions. Never silently recover an untrusted journal.

**Step 4: Verify GREEN**

Run: `python/.venv/bin/pytest python/tests/core/test_recovery.py python/tests/integration/test_fault_matrix.py -q`  
Expected: PASS.

**Step 5: Commit**

```bash
git add python/src/minimalist_installer/core/recovery.py python/tests/core/test_recovery.py python/tests/integration
git commit -m "feat(python): recover interrupted installer transactions"
```

### Task 8: Add scopes, host registry, and automatic detection

**Files:**
- Create: `python/src/minimalist_installer/skills/__init__.py`
- Create: `python/src/minimalist_installer/skills/models.py`
- Create: `python/src/minimalist_installer/skills/registry.py`
- Create: `python/src/minimalist_installer/skills/detector.py`
- Create: `python/src/minimalist_installer/skills/scope.py`
- Create: `python/src/minimalist_installer/skills/hosts/*.toml`
- Create: `python/tests/skills/test_registry.py`
- Create: `python/tests/skills/test_detection.py`
- Create: `python/tests/skills/test_scope.py`

**Step 1: Write failing registry/detection tests**

Test bundled descriptors, entry-point extensions, explicit support tiers,
executable/config/environment/project evidence, confidence ordering, shared-root
deduplication, user/project destinations, no-host behavior, git-root resolution,
and refusal of unsafe project targets.

**Step 2: Verify RED**

Run: `python/.venv/bin/pytest python/tests/skills/test_registry.py python/tests/skills/test_detection.py python/tests/skills/test_scope.py -q`  
Expected: FAIL.

**Step 3: Implement data-driven hosts**

Load TOML descriptors with `tomllib`, external adapters with
`importlib.metadata.entry_points`, and evidence with read-only stdlib probes.
Do not execute host binaries by default. Put every host ID/path outside consumer
code and outside the generic core.

**Step 4: Verify GREEN**

Run: `python/.venv/bin/pytest python/tests/skills -q`  
Expected: PASS.

**Step 5: Commit**

```bash
git add python/src/minimalist_installer/skills python/tests/skills
git commit -m "feat(python): detect agent skill hosts automatically"
```

### Task 9: Build validated skill distributions and rendering

**Files:**
- Create: `python/src/minimalist_installer/skills/bundle.py`
- Create: `python/src/minimalist_installer/skills/distribution.py`
- Create: `python/tests/skills/test_bundle.py`
- Create: `python/tests/skills/test_distribution.py`
- Create: `spec/schemas/skill-distribution-v1.schema.json`

**Step 1: Write failing bundle tests**

Cover required `SKILL.md`, YAML-frontmatter minimum without a YAML dependency,
recursive assets/references, path escape, symlink refusal, destination collision,
strict declared-variable rendering, absolute executable injection, TOML loading,
and deterministic physical-target deduplication.

**Step 2: Verify RED**

Run: `python/.venv/bin/pytest python/tests/skills/test_bundle.py python/tests/skills/test_distribution.py -q`  
Expected: FAIL.

**Step 3: Implement distribution planning**

Inventory immutable bundle bytes, render only explicit `{{VARIABLE}}` tokens,
resolve selected hosts/scopes, emit one `FileSetProvider` plan, and retain host
attribution for results even when destinations are shared.

**Step 4: Verify GREEN**

Run: `python/.venv/bin/pytest python/tests/skills -q`  
Expected: PASS.

**Step 5: Commit**

```bash
git add python/src/minimalist_installer/skills python/tests/skills spec/schemas/skill-distribution-v1.schema.json
git commit -m "feat(python): plan portable skill distributions"
```

### Task 10: Reproduce the installer TUI and expose the generic CLI

**Files:**
- Create: `python/src/minimalist_installer/tui/__init__.py`
- Create: `python/src/minimalist_installer/tui/app.py`
- Create: `python/src/minimalist_installer/tui/messages.py`
- Create: `python/src/minimalist_installer/tui/theme.py`
- Create: `python/src/minimalist_installer/cli.py`
- Create: `python/tests/tui/test_app.py`
- Create: `python/tests/test_cli.py`

**Step 1: Write failing scripted-TUI and CLI tests**

Inject prompt/console ports and verify intro, scope, detected evidence,
preselection, customization, conflict review, confirmation, cancellation,
progress, summaries, PT/EN messages, `NO_COLOR`, non-Unicode output, no-TTY
refusal, `--yes`, and versioned JSON stdout with human stderr.

**Step 2: Verify RED**

Run: `python/.venv/bin/pytest python/tests/tui/test_app.py python/tests/test_cli.py -q`  
Expected: FAIL.

**Step 3: Implement presentation adapters**

Lazy-import Rich and Questionary only for interactive mode. Keep the application
controller testable without a real terminal. Implement `install`, `update`,
`detect`, `status`, `repair`, and `uninstall` commands.

**Step 4: Verify GREEN**

Run: `python/.venv/bin/pytest python/tests/tui/test_app.py python/tests/test_cli.py -q`  
Expected: PASS.

**Step 5: Commit**

```bash
git add python/src/minimalist_installer/tui python/src/minimalist_installer/cli.py python/tests/tui python/tests/test_cli.py
git commit -m "feat(python): add agent installer TUI and CLI"
```

### Task 11: Add conformance, artifact, and cross-platform gates

**Files:**
- Create: `python/tests/conformance/test_vectors.py`
- Create: `python/tests/integration/test_artifact.py`
- Create: `python/tests/integration/test_roundtrip.py`
- Create: `spec/capability-matrix.json`
- Modify: `.github/workflows/test.yml`
- Modify: `package.json`
- Modify: `README.md`

**Step 1: Write failing artifact/conformance tests**

Build wheel and sdist, install the wheel into a clean virtualenv, exercise the
console entry point, verify bundled host descriptors, and consume every shared
vector. Add whole-tree round trips and concurrent overlap tests.

**Step 2: Verify RED**

Run: `python/.venv/bin/pytest python/tests/conformance python/tests/integration/test_artifact.py python/tests/integration/test_roundtrip.py -q`  
Expected: FAIL before packaging/data/CI integration is complete.

**Step 3: Add project-level gates and documentation**

Add Linux/macOS/Windows Python 3.11–3.13 CI, Node regression job, build checks,
Python install/API examples, support-tier table, and capability matrix. Do not
add a publishing workflow yet.

**Step 4: Verify GREEN**

Run: `npm test`  
Run: `python/.venv/bin/pytest python/tests -q`  
Run: `python/.venv/bin/python -m build python`  
Expected: all pass; wheel and sdist created.

**Step 5: Commit**

```bash
git add .github package.json README.md python spec
git commit -m "test: gate Python installer across platforms"
```

### Task 12: Integrate `lacuna-signer` as the first consumer

**Files:**
- Modify: `/home/henry/lacuna-signer/pyproject.toml`
- Modify: `/home/henry/lacuna-signer/src/lacuna_signer/cli.py`
- Create: `/home/henry/lacuna-signer/src/lacuna_signer/agent_skill/SKILL.md`
- Create: `/home/henry/lacuna-signer/src/lacuna_signer/agent_skill/references/api.md`
- Create: `/home/henry/lacuna-signer/src/lacuna_signer/skill_install.py`
- Create: `/home/henry/lacuna-signer/tests/test_skill_install.py`
- Modify: `/home/henry/lacuna-signer/README.md`
- Modify: `/home/henry/lacuna-signer/SKILL.md`

**Step 1: Create an isolated lacuna-signer feature branch and write failing tests**

Preserve unrelated `docs/plans/flow/`. Test packaged bundle presence,
`lacuna-signer skill install/status/uninstall`, automatic host detection,
absolute active executable rendering, no key persistence, round-trip removal,
and production install without the `agents` extra.

**Step 2: Verify RED**

Run: `.venv/bin/pytest tests/test_skill_install.py -q`  
Expected: FAIL because the skill command and packaged bundle do not exist.

**Step 3: Implement the consumer adapter**

Add `agents` optional dependencies, lazily import the installer, package the
skill bundle, expose nested CLI commands, and update local/production docs.
Never alter send gates or expose the API key.

**Step 4: Verify GREEN and regression suite**

Run: `.venv/bin/pytest -q`  
Expected: all existing and new tests pass.

**Step 5: Commit in the lacuna-signer feature branch**

```bash
git add pyproject.toml src tests README.md SKILL.md
git commit -m "feat: install agent skill across supported hosts"
```

### Task 13: Final adversarial review and release-readiness verification

**Files:**
- Modify as required by findings only.
- Create: `docs/reviews/2026-09-10-python-port.md`

**Step 1: Review design-to-delivery coverage**

Build a D1–D22 evidence matrix and inspect for missing API exports, untested
crash boundaries, unsafe path mutations, unsupported host claims, secrets,
dependency leakage into core, and Node regressions.

**Step 2: Run complete fresh verification**

```bash
npm test
python/.venv/bin/pytest python/tests -q
python/.venv/bin/python -m build python
git diff --check
```

In `lacuna-signer`:

```bash
.venv/bin/pytest -q
git diff --check
```

**Step 3: Fix any validated findings with focused failing tests**

Follow TDD for every behavioral correction and rerun the affected suite before
the complete gate.

**Step 4: Record evidence and commit**

```bash
git add docs/reviews python spec .github README.md package.json
git commit -m "docs: verify Python installer delivery"
```

Do not publish to PyPI or merge branches automatically. Handoff both verified
branches and artifact paths for human validation.


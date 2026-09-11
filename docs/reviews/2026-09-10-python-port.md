# Review: Python installer port + lacuna-signer consumer

**Date:** 2026-09-11  
**Reviewer role:** Task 13 adversarial release-readiness verification  
**Design:** [`docs/plans/2026-09-10-python-port-design.md`](../plans/2026-09-10-python-port-design.md)  
**Plan:** [`docs/plans/2026-09-10-python-port.md`](../plans/2026-09-10-python-port.md)

**Scopes reviewed**

| Repo | Branch | Worktree | HEAD |
|---|---|---|---|
| `minimalist-installer` | `feat/python-installer` | `/home/henry/minimalist-installer/.worktrees/python-installer` | `ba4c571` |
| `lacuna-signer` | `feat/skill-install` | `/home/henry/lacuna-signer/.worktrees/skill-install` | `55fa93e` |

**Verdict:** Release-ready for human validation. No Critical or Important defects validated during this pass. Do **not** merge or publish to PyPI from this review.

---

## 1. D1–D22 evidence matrix

| ID | Decision (short) | Evidence (path / commit) | Status |
|---|---|---|---|
| D1 | Keep JS + Python in one repo; Python under `python/` | Layout: `src/`, `test/`, `python/`; Node package untouched (`package.json`). HEAD `ba4c571`. | **met** |
| D2 | Share semantics under `spec/` | `spec/conformance/*.json`, `spec/schemas/*`, `spec/capability-matrix.json`; runner `python/tests/conformance/test_vectors.py` (`a41704c`). | **met** |
| D3 | Split `core` / `effects` / `providers` / `skills` / `tui` | `python/src/minimalist_installer/{core,effects,providers,skills,tui}/`. | **met** |
| D4 | Python 3.11+; core + skills planner stdlib-only | `python/pyproject.toml` `requires-python = ">=3.11"`, empty base `dependencies`; wheel METADATA has no hard Requires-Dist. AST scan of `core`/`effects`/`providers`/`skills`: no third-party imports. | **met** |
| D5 | Pythonic API + `define_installer()` | `python/src/minimalist_installer/__init__.py` exports; `python/tests/test_public_api.py`. | **met** |
| D6 | prepare/apply/revert; persist prepared rollback before mutation | `python/src/minimalist_installer/core/driver.py`, `journal.py`; crash matrix `python/tests/integration/test_fault_matrix.py` (`4c5443d` / recovery lineage). | **met** |
| D7 | Committed `manifest.json` separate from WAL | `core/manifest.py`, `core/journal.py` (`transactions/<id>/journal.json` + blobs). | **met** |
| D8 | Stable effect IDs; no casual occurrence-order matching | Driver requires unique ids; empty id → type name only when singleton; multi-occurrence without ids → `PlanDriftError` (`python/tests/core/test_driver.py`). Prior matching is by id. | **met** |
| D9 | Acquire complete sorted lock set before prepare/mutate | `core/locks.py`, `core/driver.py`; `test_driver_acquires_complete_sorted_resources_before_prepare_and_apply`. | **met** |
| D10 | Refuse symlink/reparse; no safe backend → fail closed | `core/path_safety.py` (`safe_filesystem_backend_status` returns `unavailable` for `win32`); tests `python/tests/core/test_path_safety.py`; capability matrix `windows-safe-filesystem-mutations` = `not-applicable` (`ba4c571`). | **met** (fail-closed; see limitations) |
| D11 | Built-ins: reconcile_file_set, json_merge, refcount, legacy_prune | `python/src/minimalist_installer/effects/`; conformance + effect tests; Node parity still green (62 tests). | **met** |
| D12 | Declarative host adapters + entry points; consumers no host paths | Bundled TOML under `skills/hosts/`; `minimalist_installer.hosts` entry points in `skills/registry.py`. lacuna-signer `skill_install.py` uses `HostRegistry` / `plan_distribution` only (no hardcoded `.claude`/`.agents` paths) — `55fa93e`. | **met** |
| D13 | Detection = confidence + evidence; never authorizes writes | `skills/detector.py` docstring + implementation; TUI preselect threshold in `tui/app.py` (`PRESELECT_MIN_CONFIDENCE = 30`); writes still require install path / `--yes`. | **met** |
| D14 | Dedupe identical physical destinations (incl. `~/.agents/skills`) | `skills/distribution.py` `plan_distribution` pending-map by `(destination_root, path)`; hosts `codex`/`gemini`/`grok` share `.agents/skills`. | **met** |
| D15 | Optional TUI via `[tui]` (Rich + Questionary) | `pyproject.toml` extras; lazy imports only inside `tui/app.py` prompt/console factories; importing `core`/`skills`/`cli` does not load Rich. | **met** |
| D16 | Non-interactive never prompts; JSON; fail if no host | `cli.py` + `tui/app.py` raise `NoHostDetectedError` / `NonInteractiveInputRequiredError`; lacuna `cmd_install` requires `--yes`. | **met** |
| D17 | User + project scope; refuse root / home-as-project / bare / unwritable | `skills/scope.py`; `python/tests/skills/test_scope.py`. | **met** |
| D18 | Support tiers `verified` / `layout-only` / `external` | `SupportTier` in `skills/models.py`; host TOMLs; README host table; install path ≠ workflow qualification. | **met** |
| D19 | PyPI name `minimalist-installer`; import `minimalist_installer` | Packaging + wheel/sdist build succeed; name/import match. **Not published** (explicit non-goal of this task). | **met** (packaging); publish deferred |
| D20 | lacuna-signer `[agents]` + `lacuna-signer skill ...` | `lacuna-signer` `pyproject.toml` agents extra; `cli.py` skill subcommands; `skill_install.py` (`55fa93e`). | **met** (local `file://` dep — see limitations) |
| D21 | Skill bundle includes refs; absolute CLI path as render var | Packaged `agent_skill/{SKILL.md,references/api.md}`; `{{LACUNA_SIGNER_BIN}}` via `resolve_lacuna_signer_bin()`; tests `tests/test_skill_install.py` (`4cbbe55`). | **met** |
| D22 | No API key requested/rendered/journaled/persisted by installer | `reject_secret_variable` in `skills/bundle.py`; distribution loader refuses `API_KEY`/`SECRET`/`TOKEN`/`PASSWORD` names; lacuna only injects `LACUNA_SIGNER_BIN`. Skill docs mention env var name only. | **met** |

### Adversarial inspection notes

| Area | Result |
|---|---|
| Missing API exports | Public engine surface in `minimalist_installer.__all__` (64 names) covered by `test_public_api.py`. Skills intentionally under `minimalist_installer.skills`. |
| Untested crash boundaries | Install/update/uninstall/repair hold points exercised in `python/tests/integration/test_fault_matrix.py` + `test_recovery.py`. Windows SIGKILL matrix intentionally skipped/fail-closed. |
| Unsafe path mutations | Symlink/reparse/escape refused; Windows backend unavailable. Held `dir_fd` listing (`2714499`). |
| Unsupported host claims | Bundled tiers match README; `layout-only` hosts not claimed as workflow-verified. |
| Secrets | Secret variable names rejected; no key material in manifests/journals from installer path. |
| Dependency leakage into core | Base wheel has zero runtime Requires-Dist; TUI deps optional and lazy. |
| Node regressions | `npm test`: 62/62 pass. |

**Minor (note only, not fixed):** `build_installer` lives in `minimalist_installer.tui.app` and is reused by the CLI. It does not import Rich/Questionary at module import time, so `[tui]` is not required for non-interactive use — but the helper’s module home is slightly misleading.

---

## 2. Fresh verification outputs

Captured 2026-09-11 from the worktrees above. Exit codes are 0 unless stated.

### minimalist-installer (`feat/python-installer`)

**`npm test`**

```text
ℹ tests 62
ℹ suites 12
ℹ pass 62
ℹ fail 0
ℹ cancelled 0
ℹ skipped 0
ℹ todo 0
ℹ duration_ms 113.457021
```

Exit code: **0**

**`python/.venv/bin/pytest python/tests -q`**

```text
........................................................................ [ 63%]
........................................................................ [ 75%]
........................................................................ [ 88%]
..................................................................       [100%]
570 passed in 29.14s
```

Exit code: **0**

**`python/.venv/bin/python -m build python`**

```text
Successfully built minimalist_installer-0.1.0.tar.gz and minimalist_installer-0.1.0-py3-none-any.whl
```

Exit code: **0**

**`git diff --check`**

```text
(no output)
```

Exit code: **0**

### lacuna-signer (`feat/skill-install`)

**`.venv/bin/pytest -q`**

```text
........................................................................ [ 51%]
...................................................................      [100%]
139 passed in 5.92s
```

Exit code: **0**

**`git diff --check`**

```text
(no output)
```

Exit code: **0**

---

## 3. Known limitations

1. **Windows fail-closed mutations** — `SafeFilesystem` classifies Win32 reparse points but reports backend `unavailable`; install/update/uninstall mutations fail closed until a handle-relative Windows backend is implemented and verified. CI still runs the Python suite on `windows-latest`; crash SIGKILL matrices remain skipped. Tracked in `spec/capability-matrix.json` (`windows-safe-filesystem-mutations` = `not-applicable`) and README Status/Test sections.

2. **Text-only skill assets in the distribution planner** — `plan_distribution` requires UTF-8 text because `reconcile_file_set` stores UTF-8 content strings. Non-UTF-8 bundle files fail closed with `InvalidDistributionError` (`skill file … must be valid UTF-8`). Bundle inventory/`render_bytes` can copy opaque bytes, but the planner path used for install does not ship binaries yet.

3. **`file://` agents dependency** — lacuna-signer’s optional `agents` extra pins  
   `minimalist-installer @ file:///home/henry/minimalist-installer/.worktrees/python-installer/python`  
   for local integration (`allow-direct-references = true`). Production must switch to a published PyPI version after human publish validation. Documented in `pyproject.toml` and `AGENTS_EXTRA_HINT`.

---

## 4. Handoff (do not merge or publish)

| Item | Value |
|---|---|
| Installer branch | `feat/python-installer` |
| Installer worktree | `/home/henry/minimalist-installer/.worktrees/python-installer` |
| Installer HEAD | `ba4c571` (or later after this docs commit) |
| Consumer branch | `feat/skill-install` |
| Consumer worktree | `/home/henry/lacuna-signer/.worktrees/skill-install` |
| Consumer HEAD | `55fa93e` |
| Built artifacts | `/home/henry/minimalist-installer/.worktrees/python-installer/python/dist/minimalist_installer-0.1.0-py3-none-any.whl` (108k), `…/minimalist_installer-0.1.0.tar.gz` (152k) |
| Review doc | `docs/reviews/2026-09-10-python-port.md` |

**Human next steps**

1. Spot-check interactive TUI on a real TTY and at least one `verified` host install/uninstall.
2. Decide PyPI publish for `minimalist-installer` 0.1.0; then retarget lacuna-signer `[agents]` away from `file://`.
3. Merge only after those checks — this review does not merge or publish.

---

## 5. Fixes from this review

None. No Critical/Important behavioral defects were validated; no TDD fix commits were required in either worktree beyond adding this review record.

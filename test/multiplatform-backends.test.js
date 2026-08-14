/**
 * Multi-platform path-safety contract.
 *
 * Guarantees the engine never depends solely on Linux /proc/self/fd: every
 * mutation path must work under the path-nofollow backend (macOS/Windows class),
 * and production sources must not hardcode /proc outside path-safety.js.
 */
import { describe, it, before, after, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import {
  mkdtempSync, mkdirSync, writeFileSync, readFileSync, symlinkSync, rmSync,
  readdirSync, existsSync, lstatSync, constants as fsConstants,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  PathSafetyError,
  ERR_UNSUPPORTED_PLATFORM,
  writeFileNoFollow,
  readFileNoFollow,
  existsNoFollow,
  unlinkNoFollow,
  openParentNoFollow,
  entryPath,
  getPathSafetyBackend,
  resetPathSafetyBackendForTests,
  defineInstaller,
  createFileSetProvider,
} from '../src/index.js';

const SRC_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');

function walkJsFiles(dir, out = []) {
  for (const ent of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, ent.name);
    if (ent.isDirectory()) walkJsFiles(p, out);
    else if (ent.isFile() && ent.name.endsWith('.js')) out.push(p);
  }
  return out;
}

function withBackend(kind, fn) {
  const prev = process.env.MINIMALIST_INSTALLER_PATH_BACKEND;
  process.env.MINIMALIST_INSTALLER_PATH_BACKEND = kind;
  resetPathSafetyBackendForTests();
  try {
    return fn();
  } finally {
    if (prev === undefined) delete process.env.MINIMALIST_INSTALLER_PATH_BACKEND;
    else process.env.MINIMALIST_INSTALLER_PATH_BACKEND = prev;
    resetPathSafetyBackendForTests();
  }
}

describe('multiplatform path backends', () => {
  let root;
  afterEach(() => {
    if (root) rmSync(root, { recursive: true, force: true });
    root = undefined;
    delete process.env.MINIMALIST_INSTALLER_PATH_BACKEND;
    resetPathSafetyBackendForTests();
  });

  it('selects a backend without throwing on this host', () => {
    const backend = getPathSafetyBackend();
    assert.ok(
      backend.kind === 'fd-relative'
        || backend.kind === 'path-nofollow'
        || backend.kind === 'windows-noreparse',
      `unexpected backend: ${JSON.stringify(backend)}`,
    );
    if (backend.kind === 'fd-relative') {
      assert.ok(
        backend.prefix === '/proc/self/fd' || backend.prefix === '/dev/fd',
        `unexpected fd prefix: ${backend.prefix}`,
      );
    }
  });

  it('forced path backend is portable (path-nofollow or windows-noreparse)', () => {
    withBackend('path', () => {
      const kind = getPathSafetyBackend().kind;
      if (typeof fsConstants.O_NOFOLLOW === 'number' && fsConstants.O_NOFOLLOW !== 0) {
        assert.equal(kind, 'path-nofollow');
      } else {
        assert.equal(process.platform, 'win32');
        assert.equal(kind, 'windows-noreparse');
      }
    });
  });

  for (const backendKind of ['path', 'proc']) {
    it(`full write/read/unlink cycle under backend=${backendKind}`, () => {
      if (backendKind === 'proc' && !existsSync('/proc/self/fd')) {
        // Host cannot force proc — skip (macOS CI).
        return;
      }
      withBackend(backendKind, () => {
        root = mkdtempSync(join(tmpdir(), 'mi-mp-cycle-'));
        const base = join(root, 'base');
        mkdirSync(base, { recursive: true });
        writeFileNoFollow(base, 'nested/file.txt', 'hello-mp', { atomic: true });
        assert.equal(readFileNoFollow(base, 'nested/file.txt'), 'hello-mp');
        assert.equal(existsNoFollow(base, 'nested/file.txt'), true);
        unlinkNoFollow(base, 'nested/file.txt');
        assert.equal(existsNoFollow(base, 'nested/file.txt'), false);
      });
    });

    it(`defineInstaller install/uninstall under backend=${backendKind}`, () => {
      if (backendKind === 'proc' && !existsSync('/proc/self/fd')) return;
      withBackend(backendKind, () => {
        root = mkdtempSync(join(tmpdir(), 'mi-mp-drv-'));
        const projectDir = join(root, 'proj');
        mkdirSync(projectDir, { recursive: true });
        const installer = defineInstaller({
          providers: [createFileSetProvider()],
          config: {
            manifestDir: '.mi-manifest',
            lockRoot: join(root, 'locks'),
            files: [
              { path: 'skills/a.md', content: 'A' },
              { path: 'deep/nested/b.md', content: 'B' },
            ],
          },
        });
        installer.install({ projectDir });
        assert.equal(readFileSync(join(projectDir, 'skills/a.md'), 'utf8'), 'A');
        assert.equal(readFileSync(join(projectDir, 'deep/nested/b.md'), 'utf8'), 'B');
        installer.uninstall({ projectDir });
        assert.equal(existsSync(join(projectDir, 'skills/a.md')), false);
        assert.equal(existsSync(join(projectDir, 'deep/nested/b.md')), false);
      });
    });

    it(`refuses leaf symlink under backend=${backendKind}`, () => {
      if (backendKind === 'proc' && !existsSync('/proc/self/fd')) return;
      withBackend(backendKind, () => {
        root = mkdtempSync(join(tmpdir(), 'mi-mp-sym-'));
        const base = join(root, 'base');
        const outside = join(root, 'out');
        mkdirSync(join(base, 'd'), { recursive: true });
        mkdirSync(outside, { recursive: true });
        const sentinel = join(outside, 'secret.txt');
        writeFileSync(sentinel, 'SAFE');
        try {
          symlinkSync(sentinel, join(base, 'd', 'f.txt'));
        } catch (err) {
          // Windows without symlink privilege — cannot exercise; not a product skip of install.
          if (err.code === 'EPERM' || err.code === 'EACCES') return;
          throw err;
        }
        assert.throws(
          () => writeFileNoFollow(base, 'd/f.txt', 'PWNED', { atomic: true }),
          (e) => e instanceof PathSafetyError && e.code === 'UNSAFE_PATH_RACE',
        );
        assert.equal(readFileSync(sentinel, 'utf8'), 'SAFE');
      });
    });
  }

  it('entryPath never returns a bare absolute join that skips O_NOFOLLOW parent open', () => {
    withBackend('path', () => {
      root = mkdtempSync(join(tmpdir(), 'mi-mp-entry-'));
      const base = join(root, 'base');
      mkdirSync(base, { recursive: true });
      const handle = openParentNoFollow(base, 'x/y.txt', { createParents: true });
      try {
        const p = entryPath(handle, handle.leafName);
        assert.ok(typeof p === 'string' && p.length > 0);
        assert.ok(p.includes(handle.leafName));
        // path backend: absolute under parentAbs
        assert.ok(p.startsWith(handle.parentAbs) || p.includes('/proc/') || p.includes('/dev/fd/'));
      } finally {
        handle.close();
      }
    });
  });

  it('this host can always select a backend', () => {
    assert.doesNotThrow(() => getPathSafetyBackend());
    assert.notEqual(getPathSafetyBackend().kind, undefined);
    assert.equal(ERR_UNSUPPORTED_PLATFORM, 'UNSUPPORTED_PLATFORM');
  });

  it('refuses an intermediate Windows junction and does not write outside', () => {
    if (process.platform !== 'win32') return;
    withBackend('path', () => {
      root = mkdtempSync(join(tmpdir(), 'mi-mp-junc-'));
      const base = join(root, 'base');
      const outside = join(root, 'out');
      mkdirSync(base, { recursive: true });
      mkdirSync(outside, { recursive: true });
      const sentinel = join(outside, 'secret.txt');
      writeFileSync(sentinel, 'SAFE');
      symlinkSync(outside, join(base, 'linked'), 'junction');
      assert.equal(lstatSync(join(base, 'linked')).isSymbolicLink(), true);
      assert.throws(
        () => writeFileNoFollow(base, 'linked/pwned.txt', 'PWNED', { atomic: true }),
        (e) => e instanceof PathSafetyError && e.code === 'UNSAFE_PATH_RACE',
      );
      assert.equal(readFileSync(sentinel, 'utf8'), 'SAFE');
      assert.equal(existsSync(join(outside, 'pwned.txt')), false);
    });
  });
});

describe('multiplatform static source guards', () => {
  it('only path-safety.js may contain /proc/self/fd or /dev/fd literals', () => {
    const files = walkJsFiles(SRC_ROOT);
    const offenders = [];
    for (const file of files) {
      if (file.endsWith(`${join('src', 'path-safety.js')}`) || file.endsWith('path-safety.js')) {
        // Allow the backend module itself.
        if (file.replace(/\\/g, '/').endsWith('/src/path-safety.js')) continue;
      }
      const rel = file.slice(SRC_ROOT.length + 1).replace(/\\/g, '/');
      if (rel === 'path-safety.js') continue;
      const text = readFileSync(file, 'utf8');
      if (text.includes('/proc/self/fd') || /['"`]\/dev\/fd\//.test(text)) {
        offenders.push(rel);
      }
    }
    assert.deepEqual(
      offenders,
      [],
      `Hardcoded fd-relative paths outside path-safety.js (use entryPath): ${offenders.join(', ')}`,
    );
  });

  it('path-safety.js declares path-nofollow backend and does not Linux-only fail-closed', () => {
    const src = readFileSync(join(SRC_ROOT, 'path-safety.js'), 'utf8');
    assert.match(src, /path-nofollow/);
    assert.match(src, /O_NOFOLLOW/);
    // Banned: the pre-fix fail-closed that only allowed Linux /proc.
    assert.doesNotMatch(
      src,
      /require \/proc\/self\/fd \(Linux\)\. Refusing permissive fallback/,
    );
    assert.match(src, /windows-noreparse/);
    // Must not exclude win32 from path-nofollow when O_NOFOLLOW exists.
    assert.doesNotMatch(
      src,
      /O_NOFOLLOW === 'number' && process\.platform !== 'win32'/,
    );
    // Must not fake kernel no-follow with a zero flag (that follows junctions).
    assert.doesNotMatch(src, /O_NOFOLLOW['"]?\s*[:=]\s*0/);
  });

  it('public API exports backend introspection for consumers/tests', () => {
    assert.equal(typeof getPathSafetyBackend, 'function');
    assert.equal(typeof resetPathSafetyBackendForTests, 'function');
    assert.equal(typeof entryPath, 'function');
  });
});

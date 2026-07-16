/**
 * No-follow path authority for installer mutations.
 *
 * Backends (selected once per process, fail-closed if none apply):
 *
 * 1. **fd-relative** — open a parent directory fd, then operate on
 *    `<prefix>/<dirfd>/<name>` with O_NOFOLLOW. Prefix is `/proc/self/fd`
 *    (Linux) or `/dev/fd` when a runtime probe proves relative child ops work
 *    (some BSDs). Holds directory identity across renames; strongest TOCTOU
 *    resistance without a native openat binding.
 *
 * 2. **path-nofollow** — component walk opening each intermediate with
 *    O_DIRECTORY|O_NOFOLLOW, then leaf ops with O_NOFOLLOW on the absolute
 *    path under the verified parent. Blocks pre-placed intermediate/leaf
 *    symlinks (the realistic installer threat). Weaker against concurrent
 *    rename races between parent open and path-based child open — used when
 *    no fd-relative mount exists (macOS, and other Unix without proc/fdesc
 *    child lookup). Never a "follow-symlinks" fallback.
 *
 * Check-then-use revalidation on path strings alone is intentionally
 * insufficient: every open of a mutable component uses O_NOFOLLOW.
 */
import {
  openSync,
  closeSync,
  writeSync,
  readSync,
  fsyncSync,
  fstatSync,
  mkdirSync,
  renameSync,
  unlinkSync,
  rmdirSync,
  readdirSync,
  constants,
  existsSync,
  mkdtempSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { join, resolve, sep, posix } from 'node:path';
import { tmpdir } from 'node:os';

export const ERR_UNSAFE_PATH_RACE = 'UNSAFE_PATH_RACE';
export const ERR_PATH_ESCAPE = 'PATH_ESCAPE';
export const ERR_GREENFIELD_CONFLICT = 'GREENFIELD_CONFLICT';
export const ERR_UNSUPPORTED_PLATFORM = 'UNSUPPORTED_PLATFORM';

export class PathSafetyError extends Error {
  /**
   * @param {string} code
   * @param {string} message
   * @param {object} [details]
   */
  constructor(code, message, details = {}) {
    super(message);
    this.name = 'PathSafetyError';
    this.details = details;
    this.code = code;
  }
}

/**
 * Probe whether `<prefix>/<dirfd>/<name>` can create/open files relative to an
 * open directory fd (Linux procfs and some fdescfs implementations).
 * @param {string} prefix
 * @returns {boolean}
 */
function probeFdRelativePrefix(prefix) {
  if (!existsSync(prefix)) return false;
  let probeRoot;
  let dirFd;
  try {
    probeRoot = mkdtempSync(join(tmpdir(), 'mi-fd-probe-'));
    dirFd = openSync(probeRoot, constants.O_RDONLY | constants.O_DIRECTORY);
    const child = `${prefix}/${dirFd}/.probe-${process.pid}`;
    writeFileSync(child, 'ok');
    unlinkSync(child);
    return true;
  } catch {
    return false;
  } finally {
    if (dirFd != null) {
      try { closeSync(dirFd); } catch { /* ignore */ }
    }
    if (probeRoot) {
      try { rmSync(probeRoot, { recursive: true, force: true }); } catch { /* ignore */ }
    }
  }
}

/**
 * @returns {{ kind: 'fd-relative', prefix: string } | { kind: 'path-nofollow' }}
 */
function detectBackend() {
  const forced = process.env.MINIMALIST_INSTALLER_PATH_BACKEND;
  if (forced === 'path' || forced === 'path-nofollow') {
    if (typeof constants.O_NOFOLLOW !== 'number') {
      throw new PathSafetyError(
        ERR_UNSUPPORTED_PLATFORM,
        'Forced path-nofollow backend requires O_NOFOLLOW.',
      );
    }
    return { kind: 'path-nofollow' };
  }
  if (forced === 'proc') {
    return { kind: 'fd-relative', prefix: '/proc/self/fd' };
  }
  if (forced === 'devfd') {
    return { kind: 'fd-relative', prefix: '/dev/fd' };
  }

  if (probeFdRelativePrefix('/proc/self/fd')) {
    return { kind: 'fd-relative', prefix: '/proc/self/fd' };
  }
  if (probeFdRelativePrefix('/dev/fd')) {
    return { kind: 'fd-relative', prefix: '/dev/fd' };
  }
  // macOS and other Unix: O_NOFOLLOW component walk (no symlink following).
  if (typeof constants.O_NOFOLLOW === 'number' && process.platform !== 'win32') {
    return { kind: 'path-nofollow' };
  }
  throw new PathSafetyError(
    ERR_UNSUPPORTED_PLATFORM,
    'No-follow mutations require an fd-relative mount (/proc/self/fd or /dev/fd) '
    + 'or Unix O_NOFOLLOW. Refusing platforms that cannot refuse symlink follow.',
  );
}

/** @type {{ kind: 'fd-relative', prefix: string } | { kind: 'path-nofollow' } | null} */
let cachedBackend = null;

export function getPathSafetyBackend() {
  if (!cachedBackend) cachedBackend = detectBackend();
  return cachedBackend;
}

/** Test-only: clear cached backend selection. */
export function resetPathSafetyBackendForTests() {
  cachedBackend = null;
}

export function assertNoFollowPlatform() {
  getPathSafetyBackend();
}

/**
 * Path of a directory entry relative to an open parent directory handle.
 * Prefer fd-relative paths; path-nofollow uses absolute parent + name.
 *
 * @param {{ parentFd: number, parentAbs: string }} handle
 * @param {string} name
 * @returns {string}
 */
export function entryPath(handle, name) {
  const backend = getPathSafetyBackend();
  if (backend.kind === 'fd-relative') {
    return `${backend.prefix}/${handle.parentFd}/${name}`;
  }
  return join(handle.parentAbs, name);
}

/**
 * Split a relative desired path into safe components (no absolute, no empty, no `.`/`..`).
 * @param {string} relativePath
 * @returns {string[]}
 */
export function splitRelativePath(relativePath) {
  if (typeof relativePath !== 'string' || relativePath.length === 0) {
    throw new PathSafetyError(ERR_PATH_ESCAPE, 'Path must be a non-empty relative string');
  }
  if (relativePath.startsWith('/') || relativePath.startsWith('\\')) {
    throw new PathSafetyError(ERR_PATH_ESCAPE, `Absolute paths are refused: "${relativePath}"`);
  }
  // Normalize to POSIX separators for component walk; reject Windows drive letters.
  if (/^[a-zA-Z]:/.test(relativePath)) {
    throw new PathSafetyError(ERR_PATH_ESCAPE, `Drive-letter paths are refused: "${relativePath}"`);
  }
  const normalized = relativePath.replace(/\\/g, '/');
  const parts = normalized.split('/').filter((p) => p.length > 0);
  if (parts.length === 0) {
    throw new PathSafetyError(ERR_PATH_ESCAPE, 'Empty path after normalization');
  }
  for (const part of parts) {
    if (part === '.' || part === '..') {
      throw new PathSafetyError(ERR_PATH_ESCAPE, `Refusing path component "${part}" in "${relativePath}"`);
    }
    if (part.includes('\0')) {
      throw new PathSafetyError(ERR_PATH_ESCAPE, 'NUL in path component');
    }
  }
  // Lexical containment vs base still required as a first line of defense.
  return parts;
}

/**
 * Lexical containment check (does not follow symlinks; pure string resolve).
 */
export function assertLexicalWithinBase(basePath, relativePath) {
  const base = resolve(basePath);
  const absPath = resolve(join(basePath, relativePath));
  if (absPath !== base && !absPath.startsWith(base + sep)) {
    throw new PathSafetyError(
      ERR_PATH_ESCAPE,
      `Refusing to operate outside basePath: "${relativePath}"`,
    );
  }
  return absPath;
}

function openDirFd(pathOrProc, { noFollow = false } = {}) {
  const flags = constants.O_RDONLY | constants.O_DIRECTORY | (noFollow ? constants.O_NOFOLLOW : 0);
  try {
    return openSync(pathOrProc, flags);
  } catch (err) {
    if (err.code === 'ELOOP' || err.code === 'ENOTDIR') {
      throw new PathSafetyError(
        ERR_UNSAFE_PATH_RACE,
        `Directory component is a symlink or reparse point: ${pathOrProc}`,
        { causeCode: err.code },
      );
    }
    throw err;
  }
}

/**
 * Open base directory. The base itself is the trust root (may be realpath'd by caller).
 */
export function openBaseDir(basePath) {
  assertNoFollowPlatform();
  const abs = resolve(basePath);
  try {
    return openSync(abs, constants.O_RDONLY | constants.O_DIRECTORY);
  } catch (err) {
    if (err.code === 'ENOENT') {
      mkdirSync(abs, { recursive: true });
      return openSync(abs, constants.O_RDONLY | constants.O_DIRECTORY);
    }
    throw err;
  }
}

/**
 * Build the path used to open a child of the current parent under the active backend.
 * @param {number} parentFd
 * @param {string} parentAbs
 * @param {string} part
 */
function childEntryPath(parentFd, parentAbs, part) {
  return entryPath({ parentFd, parentAbs }, part);
}

/**
 * Walk components under base with O_NOFOLLOW. Optionally create missing dirs.
 * Returns a handle for the parent directory of the leaf and the leaf name.
 *
 * @returns {{
 *   parentFd: number,
 *   parentAbs: string,
 *   leafName: string,
 *   close: () => void,
 *   components: string[],
 * }}
 */
export function openParentNoFollow(basePath, relativePath, { createParents = false } = {}) {
  assertNoFollowPlatform();
  assertLexicalWithinBase(basePath, relativePath);
  const components = splitRelativePath(relativePath);
  const leafName = components[components.length - 1];
  const dirParts = components.slice(0, -1);

  const fds = [];
  const baseAbs = resolve(basePath);
  const baseFd = openBaseDir(basePath);
  fds.push(baseFd);
  let currentFd = baseFd;
  let currentAbs = baseAbs;

  for (const part of dirParts) {
    const childPath = childEntryPath(currentFd, currentAbs, part);
    let childFd;
    try {
      childFd = openDirFd(childPath, { noFollow: true });
    } catch (err) {
      if (err instanceof PathSafetyError) {
        for (const fd of fds.reverse()) closeSync(fd);
        throw err;
      }
      if (err.code === 'ENOENT' && createParents) {
        try {
          mkdirSync(childPath);
        } catch (mkdirErr) {
          if (mkdirErr.code !== 'EEXIST') {
            for (const fd of fds.reverse()) closeSync(fd);
            throw mkdirErr;
          }
        }
        try {
          childFd = openDirFd(childPath, { noFollow: true });
        } catch (err2) {
          for (const fd of fds.reverse()) closeSync(fd);
          if (err2 instanceof PathSafetyError) throw err2;
          if (err2.code === 'ELOOP' || err2.code === 'ENOTDIR') {
            throw new PathSafetyError(
              ERR_UNSAFE_PATH_RACE,
              `Directory component became a symlink: "${part}"`,
              { causeCode: err2.code },
            );
          }
          throw err2;
        }
      } else if (err.code === 'ENOENT') {
        // Missing intermediate — close and rethrow ENOENT so existsNoFollow → false.
        for (const fd of fds.reverse()) {
          try { closeSync(fd); } catch { /* ignore */ }
        }
        const missing = new Error(`ENOENT: missing path component "${part}"`);
        missing.code = 'ENOENT';
        throw missing;
      } else if (err.code === 'ELOOP' || err.code === 'ENOTDIR') {
        for (const fd of fds.reverse()) closeSync(fd);
        throw new PathSafetyError(
          ERR_UNSAFE_PATH_RACE,
          `Directory component is a symlink: "${part}"`,
          { causeCode: err.code },
        );
      } else {
        for (const fd of fds.reverse()) closeSync(fd);
        throw err;
      }
    }
    fds.push(childFd);
    currentFd = childFd;
    currentAbs = join(currentAbs, part);
  }

  return {
    parentFd: currentFd,
    parentAbs: currentAbs,
    leafName,
    components,
    close() {
      for (const fd of fds.reverse()) {
        try { closeSync(fd); } catch { /* ignore */ }
      }
    },
  };
}

function mapOpenError(err, leafName) {
  if (err.code === 'ELOOP' || err.code === 'ENOTDIR') {
    return new PathSafetyError(
      ERR_UNSAFE_PATH_RACE,
      `Refusing to follow symlink at leaf "${leafName}"`,
      { causeCode: err.code },
    );
  }
  return err;
}

/**
 * @returns {boolean}
 */
export function existsNoFollow(basePath, relativePath) {
  let handle;
  try {
    handle = openParentNoFollow(basePath, relativePath, { createParents: false });
  } catch (err) {
    if (err.code === 'ENOENT') return false;
    throw err;
  }
  try {
    const p = entryPath(handle, handle.leafName);
    try {
      const fd = openSync(p, constants.O_RDONLY | constants.O_NOFOLLOW);
      closeSync(fd);
      return true;
    } catch (err) {
      if (err.code === 'ENOENT') return false;
      if (err.code === 'ELOOP') {
        // Symlink exists at leaf — treat as present (so greenfield conflict / race can fire).
        return true;
      }
      if (err.code === 'EISDIR') {
        try {
          const fd = openSync(p, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
          closeSync(fd);
          return true;
        } catch (err2) {
          if (err2.code === 'ENOENT') return false;
          throw mapOpenError(err2, handle.leafName);
        }
      }
      throw mapOpenError(err, handle.leafName);
    }
  } finally {
    handle.close();
  }
}

/**
 * Read file contents without following a leaf symlink.
 * @returns {string}
 */
export function readFileNoFollow(basePath, relativePath, encoding = 'utf8') {
  const handle = openParentNoFollow(basePath, relativePath, { createParents: false });
  try {
    const p = entryPath(handle, handle.leafName);
    let fd;
    try {
      fd = openSync(p, constants.O_RDONLY | constants.O_NOFOLLOW);
    } catch (err) {
      throw mapOpenError(err, handle.leafName);
    }
    try {
      const st = fstatSync(fd);
      if (st.isDirectory()) {
        throw new PathSafetyError(ERR_UNSAFE_PATH_RACE, `Expected file, found directory: "${relativePath}"`);
      }
      const buf = Buffer.alloc(st.size);
      let offset = 0;
      while (offset < st.size) {
        const n = readSync(fd, buf, offset, st.size - offset, offset);
        if (n === 0) break;
        offset += n;
      }
      return encoding ? buf.subarray(0, offset).toString(encoding) : buf.subarray(0, offset);
    } finally {
      closeSync(fd);
    }
  } finally {
    handle.close();
  }
}

/**
 * Write file contents without following leaf or intermediate symlinks.
 * Uses temp file in the same directory + rename for atomic replace when replace=true.
 */
export function writeFileNoFollow(basePath, relativePath, content, {
  exclusive = false,
  mode = 0o644,
  atomic = true,
} = {}) {
  const handle = openParentNoFollow(basePath, relativePath, { createParents: true });
  try {
    const data = typeof content === 'string' ? Buffer.from(content, 'utf8') : content;
    if (!atomic || exclusive) {
      const flags = constants.O_WRONLY | constants.O_CREAT | constants.O_NOFOLLOW
        | (exclusive ? constants.O_EXCL : constants.O_TRUNC);
      const p = entryPath(handle, handle.leafName);
      let fd;
      try {
        fd = openSync(p, flags, mode);
      } catch (err) {
        throw mapOpenError(err, handle.leafName);
      }
      try {
        writeSync(fd, data);
        fsyncSync(fd);
      } finally {
        closeSync(fd);
      }
      return;
    }

    // Atomic: write temp in same dir, fsync, rename over target (rename fails if target is dir;
    // O_NOFOLLOW on temp create; rename of temp→dest where dest is symlink replaces the symlink entry).
    const tmpName = `.${handle.leafName}.tmp-${process.pid}-${Date.now()}`;
    const tmpProc = entryPath(handle, tmpName);
    let fd;
    try {
      fd = openSync(tmpProc, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, mode);
    } catch (err) {
      throw mapOpenError(err, tmpName);
    }
    try {
      writeSync(fd, data);
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
    try {
      // If leaf is a symlink, rename replaces the symlink inode in the directory
      // (does not follow it) — correct fail-closed ownership of the directory entry.
      // But we refuse to overwrite a symlink leaf that redirects outside: detect via
      // open O_NOFOLLOW first when the entry exists.
      try {
        const existing = openSync(
          entryPath(handle, handle.leafName),
          constants.O_RDONLY | constants.O_NOFOLLOW,
        );
        closeSync(existing);
      } catch (err) {
        if (err.code === 'ELOOP') {
          try { unlinkSync(tmpProc); } catch { /* ignore */ }
          throw new PathSafetyError(
            ERR_UNSAFE_PATH_RACE,
            `Refusing to replace symlink leaf "${handle.leafName}"`,
            { causeCode: err.code },
          );
        }
        if (err.code !== 'ENOENT') {
          // EISDIR etc.
          if (err.code === 'EISDIR') {
            // fall through — rename will fail appropriately
          } else {
            try { unlinkSync(tmpProc); } catch { /* ignore */ }
            throw mapOpenError(err, handle.leafName);
          }
        }
      }
      renameSync(tmpProc, entryPath(handle, handle.leafName));
    } catch (err) {
      try { unlinkSync(tmpProc); } catch { /* ignore */ }
      if (err instanceof PathSafetyError) throw err;
      throw mapOpenError(err, handle.leafName);
    }
  } finally {
    handle.close();
  }
}

export function unlinkNoFollow(basePath, relativePath) {
  const handle = openParentNoFollow(basePath, relativePath, { createParents: false });
  try {
    const p = entryPath(handle, handle.leafName);
    // Refuse to operate if leaf is a symlink (unlink would remove the link itself,
    // which is actually safe for not following — but for prune of "our" files we
    // only delete regular files we can open with O_NOFOLLOW).
    try {
      const fd = openSync(p, constants.O_RDONLY | constants.O_NOFOLLOW);
      closeSync(fd);
    } catch (err) {
      if (err.code === 'ENOENT') return;
      if (err.code === 'ELOOP') {
        throw new PathSafetyError(
          ERR_UNSAFE_PATH_RACE,
          `Refusing to unlink symlink leaf "${handle.leafName}"`,
          { causeCode: err.code },
        );
      }
      // directories: use rmdir
      if (err.code === 'EISDIR' || err.code === 'ENOTDIR') {
        // continue to unlink/rmdir
      } else {
        throw mapOpenError(err, handle.leafName);
      }
    }
    try {
      unlinkSync(p);
    } catch (err) {
      if (err.code === 'EISDIR' || err.code === 'EPERM') {
        rmdirSync(p);
      } else if (err.code !== 'ENOENT') {
        throw err;
      }
    }
  } finally {
    handle.close();
  }
}

/**
 * Atomic write of a JSON document under basePath/relativePath (same-dir temp+rename).
 */
export function atomicWriteJsonNoFollow(basePath, relativePath, value) {
  const body = `${JSON.stringify(value, null, 2)}\n`;
  writeFileNoFollow(basePath, relativePath, body, { atomic: true });
}

/**
 * Prune empty parent directories from leaf up to (but not removing) basePath,
 * without following symlinks on any component.
 */
export function pruneEmptyParentsNoFollow(basePath, relativePath) {
  assertNoFollowPlatform();
  assertLexicalWithinBase(basePath, relativePath);
  const components = splitRelativePath(relativePath);
  // Walk from full parent path down to base, removing empty dirs.
  for (let depth = components.length - 1; depth >= 1; depth--) {
    const dirRel = components.slice(0, depth).join('/');
    try {
      const handle = openParentNoFollow(basePath, dirRel, { createParents: false });
      try {
        const p = entryPath(handle, handle.leafName);
        let fd;
        try {
          fd = openSync(p, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
        } catch (err) {
          if (err.code === 'ENOENT') continue;
          if (err.code === 'ELOOP' || err.code === 'ENOTDIR') {
            throw new PathSafetyError(
              ERR_UNSAFE_PATH_RACE,
              `Refusing to prune through symlink "${handle.leafName}"`,
              { causeCode: err.code },
            );
          }
          throw err;
        }
        try {
          // readdir via path is fine only for emptiness check after open proved not symlink —
          // still use entry path for rmdir.
          const entries = readdirSync(p);
          if (entries.length === 0) {
            closeSync(fd);
            fd = null;
            rmdirSync(p);
          }
        } finally {
          if (fd != null) closeSync(fd);
        }
      } finally {
        handle.close();
      }
    } catch (err) {
      if (err instanceof PathSafetyError) throw err;
      break;
    }
  }
}

// re-export posix for tests
export { posix };

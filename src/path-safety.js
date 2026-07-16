/**
 * No-follow path authority for installer mutations.
 *
 * All writes/unlinks/renames go through directory-handle relative operations
 * (Linux: /proc/self/fd/<dirfd>/<name> with O_NOFOLLOW). Intermediate and leaf
 * components that are symlinks fail closed with UNSAFE_PATH_RACE. Platforms
 * without /proc/self/fd fail closed — no permissive fallback.
 *
 * Check-then-use revalidation on path strings is intentionally insufficient:
 * mutations never use a post-validation absolute path for the kernel op.
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
} from 'node:fs';
import { join, resolve, sep, posix } from 'node:path';

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
    this.code = code;
    this.details = details;
  }
}

const hasProcFd = () => existsSync('/proc/self/fd');

export function assertNoFollowPlatform() {
  if (!hasProcFd()) {
    throw new PathSafetyError(
      ERR_UNSUPPORTED_PLATFORM,
      'No-follow mutations require /proc/self/fd (Linux). Refusing permissive fallback.',
    );
  }
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

function procPath(dirFd, name) {
  return `/proc/self/fd/${dirFd}/${name}`;
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
 * Walk components under base with O_NOFOLLOW. Optionally create missing dirs.
 * Returns a handle for the parent directory of the leaf and the leaf name.
 *
 * @returns {{ parentFd: number, leafName: string, close: () => void, components: string[] }}
 */
export function openParentNoFollow(basePath, relativePath, { createParents = false } = {}) {
  assertNoFollowPlatform();
  assertLexicalWithinBase(basePath, relativePath);
  const components = splitRelativePath(relativePath);
  const leafName = components[components.length - 1];
  const dirParts = components.slice(0, -1);

  const fds = [];
  const baseFd = openBaseDir(basePath);
  fds.push(baseFd);
  let currentFd = baseFd;

  for (const part of dirParts) {
    const childProc = procPath(currentFd, part);
    let childFd;
    try {
      childFd = openDirFd(childProc, { noFollow: true });
    } catch (err) {
      if (err instanceof PathSafetyError) {
        for (const fd of fds.reverse()) closeSync(fd);
        throw err;
      }
      if (err.code === 'ENOENT' && createParents) {
        try {
          mkdirSync(childProc);
        } catch (mkdirErr) {
          if (mkdirErr.code !== 'EEXIST') {
            for (const fd of fds.reverse()) closeSync(fd);
            throw mkdirErr;
          }
        }
        try {
          childFd = openDirFd(childProc, { noFollow: true });
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
  }

  return {
    parentFd: currentFd,
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
    const p = procPath(handle.parentFd, handle.leafName);
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
    const p = procPath(handle.parentFd, handle.leafName);
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
      const p = procPath(handle.parentFd, handle.leafName);
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
    const tmpProc = procPath(handle.parentFd, tmpName);
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
          procPath(handle.parentFd, handle.leafName),
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
      renameSync(tmpProc, procPath(handle.parentFd, handle.leafName));
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
    const p = procPath(handle.parentFd, handle.leafName);
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
        const p = procPath(handle.parentFd, handle.leafName);
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
          // still use proc path for rmdir.
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

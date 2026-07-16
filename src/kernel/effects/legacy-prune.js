import {
  assertLexicalWithinBase,
  existsNoFollow,
  readFileNoFollow,
  writeFileNoFollow,
  unlinkNoFollow,
  openParentNoFollow,
  entryPath,
  PathSafetyError,
  splitRelativePath,
} from '../../path-safety.js';
import {
  openSync,
  closeSync,
  readdirSync,
  rmdirSync,
  constants,
} from 'node:fs';
import { join } from 'node:path';

/**
 * Prune empty parents from leaf up to and including namespaceRootRel (relative to base),
 * but never above it. Mirrors the pre-no-follow pruneEmptyParentsWithin contract.
 */
const pruneEmptyParentsWithin = (basePath, fileRel, namespaceRootRel) => {
  const fileParts = splitRelativePath(fileRel);
  const rootParts = splitRelativePath(namespaceRootRel);
  // Walk from parent-of-file up to namespace root (inclusive).
  for (let depth = fileParts.length - 1; depth >= rootParts.length; depth--) {
    const dirRel = fileParts.slice(0, depth).join('/');
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
              'UNSAFE_PATH_RACE',
              `Refusing to prune through symlink "${handle.leafName}"`,
              { causeCode: err.code },
            );
          }
          throw err;
        }
        try {
          const entries = readdirSync(p);
          if (entries.length === 0) {
            closeSync(fd);
            fd = null;
            rmdirSync(p);
          } else {
            break;
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
};

const readFrontmatterName = (content) => {
  const head = content.slice(0, 4096);
  if (!head.startsWith('---\n')) return undefined;
  const end = head.indexOf('\n---\n', 4);
  if (end < 0) return undefined;

  const fm = head.slice(4, end);
  const match = fm.match(/^name:\s*['"]?([a-z][a-z0-9-]*)['"]?\s*$/m);
  return match?.[1];
};

/**
 * Walk files under relative root using no-follow directory opens.
 * Skips symlink components entirely.
 */
const walkFilesNoFollow = (basePath, rootRel, visitFile) => {
  const walkDir = (dirRel) => {
    // Open dir itself: for root of walk, open parent of last component.
    let entries;
    try {
      if (dirRel === '' || dirRel === '.') {
        // Should not happen — roots are always under base
        return;
      }
      const handle = openParentNoFollow(basePath, dirRel, { createParents: false });
      try {
        const p = entryPath(handle, handle.leafName);
        let fd;
        try {
          fd = openSync(p, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
        } catch (err) {
          if (err.code === 'ENOENT') return;
          if (err.code === 'ELOOP' || err.code === 'ENOTDIR') {
            throw new PathSafetyError(
              'UNSAFE_PATH_RACE',
              `Refusing to walk symlink directory "${dirRel}"`,
              { causeCode: err.code },
            );
          }
          throw err;
        }
        try {
          entries = readdirSync(p, { withFileTypes: true });
        } finally {
          closeSync(fd);
        }
      } finally {
        handle.close();
      }
    } catch (err) {
      if (err && err.code === 'ENOENT') return;
      throw err;
    }

    for (const entry of entries) {
      const childRel = join(dirRel, entry.name).replace(/\\/g, '/');
      if (entry.isSymbolicLink()) {
        // Do not follow or prune through symlinks.
        continue;
      }
      if (entry.isDirectory()) {
        walkDir(childRel);
      } else if (entry.isFile()) {
        visitFile(childRel);
      }
    }
  };

  if (!existsNoFollow(basePath, rootRel)) return;
  walkDir(rootRel);
};

export const createLegacyPruneEffect = () => ({
  type: 'legacyPrune',

  apply({ basePath, legacyNamespaceDirs, namespaceName, knownNames }) {
    const pruned = [];
    for (const dir of legacyNamespaceDirs) {
      const rootPath = join(dir, namespaceName).replace(/\\/g, '/');
      assertLexicalWithinBase(basePath, rootPath);
      if (!existsNoFollow(basePath, rootPath)) continue;

      walkFilesNoFollow(basePath, rootPath, (relativePath) => {
        assertLexicalWithinBase(basePath, relativePath);
        let content;
        try {
          content = readFileNoFollow(basePath, relativePath, 'utf8');
        } catch (err) {
          if (err instanceof PathSafetyError) return;
          // Unreadable files (EACCES) are left in place — same as pre-no-follow behavior.
          if (err && (err.code === 'EACCES' || err.code === 'EPERM')) return;
          throw err;
        }
        const name = readFrontmatterName(content);
        if (!knownNames.has(name)) return;

        unlinkNoFollow(basePath, relativePath);
        pruned.push({ path: relativePath, content });
        pruneEmptyParentsWithin(basePath, relativePath, rootPath);
      });
    }

    return { pruned };
  },

  revert({ basePath }, beforeState) {
    for (const { path, content } of beforeState.pruned) {
      assertLexicalWithinBase(basePath, path);
      writeFileNoFollow(basePath, path, content, { atomic: true });
    }
  },
});

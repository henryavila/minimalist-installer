import { hashContent } from '../hash.js';
import {
  PathSafetyError,
  ERR_GREENFIELD_CONFLICT,
  assertLexicalWithinBase,
  existsNoFollow,
  readFileNoFollow,
  writeFileNoFollow,
  unlinkNoFollow,
  pruneEmptyParentsNoFollow,
} from '../path-safety.js';

// Refuse any desired path that resolves outside basePath (lexical) and perform
// all mutations through the no-follow path authority (path-safety.js).

export const classifyFile = ({ installedHash, currentHash, newHash }) => {
  if (currentHash === installedHash) {
    return 'unchanged';
  }

  if (currentHash === newHash) {
    // Desired content already on disk (e.g. retry after partial update).
    return 'already-desired';
  }

  if (installedHash === newHash) {
    return 'keep-local';
  }

  return 'conflict';
};

export const createReconcileFileSetEffect = () => ({
  type: 'reconcileFileSet',

  // `previous` is the beforeState of the prior apply (the previously-installed
  // file set). On a greenfield install it is empty and every desired file is
  // written only when no unowned content already exists. On update it drives
  // the non-interactive 3-hash policy: a file the user modified since we
  // installed it is kept as-is (no clobber); files dropped from the desired set
  // are removed only when still unmodified (no proof-less deletion of user content).
  apply({ basePath, desired, previous = [] }) {
    const prevHashByPath = new Map(
      previous.map(({ path, installedHash }) => [path, installedHash]),
    );
    const desiredPaths = new Set(desired.map(({ path }) => path));
    const beforeState = [];

    for (const { path, content } of desired) {
      assertLexicalWithinBase(basePath, path);
      const newHash = hashContent(content);
      const prevHash = prevHashByPath.get(path);
      const present = existsNoFollow(basePath, path);

      if (present) {
        // Symlink leaf or regular file — attempt a no-follow read. Symlink leaf
        // throws UNSAFE_PATH_RACE from readFileNoFollow.
        let currentHash;
        try {
          currentHash = hashContent(readFileNoFollow(basePath, path, 'utf8'));
        } catch (err) {
          if (err instanceof PathSafetyError) throw err;
          throw err;
        }

        if (prevHash !== undefined) {
          const disposition = classifyFile({
            installedHash: prevHash,
            currentHash,
            newHash,
          });
          if (disposition === 'already-desired') {
            beforeState.push({ path, installedHash: newHash });
            continue;
          }
          if (disposition === 'keep-local' || disposition === 'conflict') {
            // User edit (or conflict): never clobber; keep tracking original hash
            // so revert will not delete user content (P3).
            beforeState.push({ path, installedHash: prevHash });
            continue;
          }
          // unchanged → fall through to rewrite (idempotent content match)
        } else {
          // Greenfield path: pre-existing content without ownership proof is a conflict.
          if (currentHash === newHash) {
            // Identical content may be adopted as already-desired only when it
            // matches desired bytes — still no ownership proof for deletion, but
            // install may proceed tracking the hash.
            beforeState.push({ path, installedHash: newHash });
            continue;
          }
          throw new PathSafetyError(
            ERR_GREENFIELD_CONFLICT,
            `Refusing to clobber unowned pre-existing file: "${path}"`,
            { path },
          );
        }
      }

      writeFileNoFollow(basePath, path, content, { atomic: true });
      beforeState.push({ path, installedHash: newHash });
    }

    for (const { path, installedHash } of previous) {
      if (desiredPaths.has(path)) continue;
      assertLexicalWithinBase(basePath, path);
      if (!existsNoFollow(basePath, path)) continue;
      let currentHash;
      try {
        currentHash = hashContent(readFileNoFollow(basePath, path, 'utf8'));
      } catch (err) {
        if (err instanceof PathSafetyError && err.code === 'UNSAFE_PATH_RACE') {
          // Do not prune through a symlink leaf.
          continue;
        }
        throw err;
      }
      if (currentHash === installedHash) {
        unlinkNoFollow(basePath, path);
        pruneEmptyParentsNoFollow(basePath, path);
      }
    }

    return beforeState;
  },

  revert({ basePath }, beforeState) {
    for (const { path, installedHash } of beforeState) {
      assertLexicalWithinBase(basePath, path);
      if (!existsNoFollow(basePath, path)) continue;

      let currentHash;
      try {
        currentHash = hashContent(readFileNoFollow(basePath, path, 'utf8'));
      } catch (err) {
        if (err instanceof PathSafetyError && err.code === 'UNSAFE_PATH_RACE') {
          continue;
        }
        throw err;
      }
      if (currentHash === installedHash) {
        unlinkNoFollow(basePath, path);
        pruneEmptyParentsNoFollow(basePath, path);
      }
    }
  },
});

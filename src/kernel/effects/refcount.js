import {
  openSync,
  closeSync,
  readdirSync,
  rmdirSync,
  constants,
  existsSync,
} from 'node:fs';
import { join } from 'node:path';

import { hashContent } from '../../hash.js';
import {
  assertLexicalWithinBase,
  existsNoFollow,
  readFileNoFollow,
  writeFileNoFollow,
  unlinkNoFollow,
  pruneEmptyParentsNoFollow,
  openParentNoFollow,
  entryPath,
  PathSafetyError,
} from '../../path-safety.js';

const listDirNoFollow = (basePath, dirRel) => {
  const handle = openParentNoFollow(basePath, dirRel, { createParents: false });
  try {
    const p = entryPath(handle, handle.leafName);
    let fd;
    try {
      fd = openSync(p, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
    } catch (err) {
      if (err.code === 'ENOENT') return null;
      if (err.code === 'ELOOP' || err.code === 'ENOTDIR') {
        throw new PathSafetyError(
          'UNSAFE_PATH_RACE',
          `Refusing to list symlink directory "${dirRel}"`,
          { causeCode: err.code },
        );
      }
      throw err;
    }
    try {
      return readdirSync(p);
    } finally {
      closeSync(fd);
    }
  } finally {
    handle.close();
  }
};

const pruneOrphanMarkers = (basePath, ownersDir) => {
  const markers = listDirNoFollow(basePath, ownersDir);
  if (!markers) return;
  for (const marker of markers) {
    const markerRel = join(ownersDir, marker).replace(/\\/g, '/');
    let ownerManifestPath;
    try {
      ownerManifestPath = readFileNoFollow(basePath, markerRel, 'utf8').trim();
    } catch {
      continue;
    }
    // ownerManifestPath is absolute and outside the install base — plain exists is OK.
    if (!existsSync(ownerManifestPath)) {
      unlinkNoFollow(basePath, markerRel);
    }
  }
};

export const createRefcountEffect = () => ({
  type: 'refcount',

  apply({ basePath, ownersDir, ownerId, ownerManifestPath }) {
    assertLexicalWithinBase(basePath, ownersDir);
    const ownerKey = hashContent(ownerId);
    const markerRel = join(ownersDir, ownerKey).replace(/\\/g, '/');
    assertLexicalWithinBase(basePath, markerRel);
    const markerExisted = existsNoFollow(basePath, markerRel);

    writeFileNoFollow(basePath, markerRel, `${ownerManifestPath}\n`, { atomic: true });

    return { ownerKey, markerExisted, ownersDir };
  },

  revert({ basePath }, beforeState) {
    const ownersDir = beforeState.ownersDir;
    assertLexicalWithinBase(basePath, ownersDir);

    if (!beforeState.markerExisted) {
      const markerRel = join(ownersDir, beforeState.ownerKey).replace(/\\/g, '/');
      if (existsNoFollow(basePath, markerRel)) {
        unlinkNoFollow(basePath, markerRel);
      }
    }

    if (existsNoFollow(basePath, ownersDir)) {
      pruneOrphanMarkers(basePath, ownersDir);

      const remaining = listDirNoFollow(basePath, ownersDir);
      if (remaining && remaining.length === 0) {
        const handle = openParentNoFollow(basePath, ownersDir, { createParents: false });
        try {
          rmdirSync(entryPath(handle, handle.leafName));
        } finally {
          handle.close();
        }
        pruneEmptyParentsNoFollow(basePath, ownersDir);
        return { lastOwnerReleased: true };
      }
    }

    return { lastOwnerReleased: false };
  },
});

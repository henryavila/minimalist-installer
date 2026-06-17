import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmdirSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { dirname, join, resolve, sep } from 'node:path';

import { hashContent } from '../../hash.js';

const resolveWithinBase = (basePath, path) => {
  const base = resolve(basePath);
  const absPath = join(basePath, path);
  const resolved = resolve(absPath);
  if (resolved !== base && !resolved.startsWith(base + sep)) {
    throw new Error(`Refusing to operate outside basePath: "${path}"`);
  }
  return absPath;
};

const pruneEmptyParents = (absPath, basePath) => {
  const base = resolve(basePath);
  let parent = dirname(resolve(absPath));

  while (parent !== base && parent !== '.') {
    try {
      if (readdirSync(parent).length === 0) {
        rmdirSync(parent);
        parent = dirname(parent);
      } else {
        break;
      }
    } catch {
      break;
    }
  }
};

const pruneOrphanMarkers = (absOwnersDir) => {
  for (const marker of readdirSync(absOwnersDir)) {
    const markerPath = join(absOwnersDir, marker);
    const ownerManifestPath = readFileSync(markerPath, 'utf8').trim();

    if (!existsSync(ownerManifestPath)) {
      unlinkSync(markerPath);
    }
  }
};

export const createRefcountEffect = () => ({
  type: 'refcount',

  apply({ basePath, ownersDir, ownerId, ownerManifestPath }) {
    const absOwnersDir = resolveWithinBase(basePath, ownersDir);
    const ownerKey = hashContent(ownerId);
    const markerPath = join(absOwnersDir, ownerKey);
    const markerExisted = existsSync(markerPath);

    mkdirSync(absOwnersDir, { recursive: true });
    writeFileSync(markerPath, `${ownerManifestPath}\n`, 'utf8');

    return { ownerKey, markerExisted, ownersDir };
  },

  revert({ basePath, ownersDir }, beforeState) {
    const absOwnersDir = resolveWithinBase(basePath, ownersDir);

    if (!beforeState.markerExisted) {
      const markerPath = join(absOwnersDir, beforeState.ownerKey);
      if (existsSync(markerPath)) {
        unlinkSync(markerPath);
      }
    }

    if (existsSync(absOwnersDir)) {
      pruneOrphanMarkers(absOwnersDir);

      if (readdirSync(absOwnersDir).length === 0) {
        rmdirSync(absOwnersDir);
        pruneEmptyParents(absOwnersDir, basePath);
        return { lastOwnerReleased: true };
      }
    }

    return { lastOwnerReleased: false };
  },
});

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

import { hashContent } from '../hash.js';

// Refuse any desired path that resolves outside basePath (e.g. a `..` segment).
// The reconciler writes and unlinks real files, so an escaping path would mutate
// the user's filesystem outside the install root — the data-safety invariant the
// reversible model assumes cannot happen.
const resolveWithinBase = (basePath, path) => {
  const base = resolve(basePath);
  const absPath = join(basePath, path);
  const resolved = resolve(absPath);
  if (resolved !== base && !resolved.startsWith(base + sep)) {
    throw new Error(`Refusing to operate outside basePath: "${path}"`);
  }
  return absPath;
};

export const classifyFile = ({ installedHash, currentHash, newHash }) => {
  if (currentHash === installedHash) {
    return 'unchanged';
  }

  if (installedHash === newHash) {
    return 'keep-local';
  }

  return 'conflict';
};

const pruneEmptyParents = (absPath, basePath) => {
  let parent = dirname(absPath);

  while (parent !== basePath && parent !== '.') {
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

export const createReconcileFileSetEffect = () => ({
  type: 'reconcileFileSet',

  // `previous` is the beforeState of the prior apply (the previously-installed
  // file set). On a greenfield install it is empty and every desired file is
  // written. On update it drives the non-interactive 3-hash policy ported from
  // the legacy installer: a file the user modified since we installed it is kept
  // as-is (no clobber); files dropped from the desired set are removed only when
  // still unmodified (no proof-less deletion of user content).
  apply({ basePath, desired, previous = [] }) {
    const prevHashByPath = new Map(
      previous.map(({ path, installedHash }) => [path, installedHash]),
    );
    const desiredPaths = new Set(desired.map(({ path }) => path));
    const beforeState = [];

    for (const { path, content } of desired) {
      const absPath = resolveWithinBase(basePath, path);
      const prevHash = prevHashByPath.get(path);

      if (prevHash !== undefined && existsSync(absPath)) {
        const currentHash = hashContent(readFileSync(absPath, 'utf8'));
        if (currentHash !== prevHash) {
          // User edited a file we installed — keep theirs (no clobber), and keep
          // tracking the ORIGINAL installed hash so the file reads as "modified"
          // forever. revert() only deletes when disk == tracked hash, so the
          // user's edits survive uninstall too (P3 — no proof-less deletion of
          // user content; symmetric with a user-modified orphan).
          beforeState.push({ path, installedHash: prevHash });
          continue;
        }
      }

      mkdirSync(dirname(absPath), { recursive: true });
      writeFileSync(absPath, content, 'utf8');
      beforeState.push({ path, installedHash: hashContent(content) });
    }

    for (const { path, installedHash } of previous) {
      if (desiredPaths.has(path)) continue;
      const absPath = resolveWithinBase(basePath, path);
      if (!existsSync(absPath)) continue;
      if (hashContent(readFileSync(absPath, 'utf8')) === installedHash) {
        unlinkSync(absPath);
        pruneEmptyParents(absPath, basePath);
      }
    }

    return beforeState;
  },

  revert({ basePath }, beforeState) {
    for (const { path, installedHash } of beforeState) {
      const absPath = resolveWithinBase(basePath, path);
      if (!existsSync(absPath)) continue;

      const currentHash = hashContent(readFileSync(absPath, 'utf8'));
      if (currentHash === installedHash) {
        unlinkSync(absPath);
        pruneEmptyParents(absPath, basePath);
      }
    }
  },
});

import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmdirSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { dirname, join, relative, resolve, sep } from 'node:path';

const resolveWithinBase = (basePath, path) => {
  const base = resolve(basePath);
  const absPath = join(basePath, path);
  const resolved = resolve(absPath);
  if (resolved !== base && !resolved.startsWith(base + sep)) {
    throw new Error(`Refusing to operate outside basePath: "${path}"`);
  }
  return absPath;
};

const pruneEmptyParentsWithin = (absPath, namespaceRoot) => {
  const root = resolve(namespaceRoot);
  let parent = dirname(resolve(absPath));

  while (parent !== root && parent.startsWith(root + sep)) {
    try {
      if (readdirSync(parent).length === 0) {
        rmdirSync(parent);
        parent = dirname(parent);
      } else {
        return;
      }
    } catch {
      return;
    }
  }

  if (parent === root) {
    try {
      if (readdirSync(root).length === 0) {
        rmdirSync(root);
      }
    } catch {
      // Preserve non-empty or already-removed roots.
    }
  }
};

const readFrontmatterName = (absPath) => {
  let head;
  try {
    head = readFileSync(absPath, 'utf8').slice(0, 4096);
  } catch {
    return undefined;
  }

  if (!head.startsWith('---\n')) return undefined;
  const end = head.indexOf('\n---\n', 4);
  if (end < 0) return undefined;

  const fm = head.slice(4, end);
  const match = fm.match(/^name:\s*['"]?([a-z][a-z0-9-]*)['"]?\s*$/m);
  return match?.[1];
};

const walkFiles = (absDir, visitFile) => {
  for (const entry of readdirSync(absDir, { withFileTypes: true })) {
    const absEntry = join(absDir, entry.name);
    if (entry.isDirectory()) {
      walkFiles(absEntry, visitFile);
    } else if (entry.isFile()) {
      visitFile(absEntry);
    }
  }
};

export const createLegacyPruneEffect = () => ({
  type: 'legacyPrune',

  apply({ basePath, legacyNamespaceDirs, namespaceName, knownNames }) {
    const pruned = [];
    for (const dir of legacyNamespaceDirs) {
      const rootPath = join(dir, namespaceName);
      const absRoot = resolveWithinBase(basePath, rootPath);
      if (!existsSync(absRoot)) continue;

      walkFiles(absRoot, (absPath) => {
        const relativePath = relative(resolve(basePath), resolve(absPath));
        resolveWithinBase(basePath, relativePath);

        const name = readFrontmatterName(absPath);
        if (!knownNames.has(name)) return;

        const content = readFileSync(absPath, 'utf8');
        unlinkSync(absPath);
        pruned.push({ path: relativePath, content });
        pruneEmptyParentsWithin(absPath, absRoot);
      });
    }

    return { pruned };
  },

  revert({ basePath }, beforeState) {
    for (const { path, content } of beforeState.pruned) {
      const absPath = resolveWithinBase(basePath, path);
      mkdirSync(dirname(absPath), { recursive: true });
      writeFileSync(absPath, content, 'utf8');
    }
  },
});

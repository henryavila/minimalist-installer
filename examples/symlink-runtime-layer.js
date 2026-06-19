// Worked example: a runtime layer that adds a NEW reversible effect type without
// touching the kernel (T-F2-4). A consumer registers `createSymlinkEffect()` via
// `defineInstaller({ effects: [...] })` and emits it from `createSymlinkProvider()`.
// Uninstall reverts it structurally, like any built-in — the consumer writes no
// uninstall logic, only the effect's own apply/revert.
//
// This lives outside the package's built-in catalog on purpose: it demonstrates
// the escape hatch. It also shows the engine's data-safety discipline applied to
// a non-file mutation — revert only removes a symlink the effect created AND that
// still points where the effect pointed it (no proof-less deletion).

import {
  existsSync,
  lstatSync,
  mkdirSync,
  readdirSync,
  readlinkSync,
  rmdirSync,
  symlinkSync,
  unlinkSync,
} from 'node:fs';
import { dirname, join, resolve, sep } from 'node:path';

const resolveWithinBase = (basePath, path) => {
  const base = resolve(basePath);
  const abs = resolve(join(basePath, path));
  if (abs !== base && !abs.startsWith(base + sep)) {
    throw new Error(`Refusing to operate outside basePath: "${path}"`);
  }
  return abs;
};

// True if any entry exists at the path — including a dangling symlink, which
// existsSync() would miss because it follows the link.
const entryExists = (absPath) => {
  try {
    lstatSync(absPath);
    return true;
  } catch {
    return false;
  }
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

export const createSymlinkEffect = () => ({
  type: 'symlink',

  apply({ basePath, links }) {
    return links.map(({ from, to }) => {
      const linkPath = resolveWithinBase(basePath, from);
      const existed = entryExists(linkPath);

      // No clobber: if something is already there we leave it and record that we
      // did not create it, so revert never removes a pre-existing entry.
      if (!existed) {
        mkdirSync(dirname(linkPath), { recursive: true });
        symlinkSync(to, linkPath);
      }

      return { from, to, existed };
    });
  },

  revert({ basePath }, beforeState) {
    for (const { from, to, existed } of beforeState) {
      if (existed) continue; // we didn't create it — never our place to remove it

      const linkPath = resolveWithinBase(basePath, from);
      if (!entryExists(linkPath)) continue;

      const stat = lstatSync(linkPath);
      // Only reclaim a symlink still pointing where we created it. If the user
      // repointed or replaced it, leave it (no proof-less deletion).
      if (stat.isSymbolicLink() && readlinkSync(linkPath) === to) {
        unlinkSync(linkPath);
        pruneEmptyParents(linkPath, basePath);
      }
    }
  },
});

// Pure planner: maps `config.links` (`[{ from, to }]`) to a single symlink effect.
export const createSymlinkProvider = () => ({
  plan(config, { basePath }) {
    const links = config.links ?? [];
    return [{ type: 'symlink', args: { basePath, links } }];
  },
});

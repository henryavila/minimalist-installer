import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';

import { createLegacyPruneEffect } from '../../../src/kernel/effects/legacy-prune.js';

describe('legacyPrune effect', () => {
  let tempDir;

  afterEach(() => {
    if (tempDir) {
      rmSync(tempDir, { recursive: true, force: true });
      tempDir = undefined;
    }
  });

  const createTempDir = () => {
    tempDir = mkdtempSync(join(tmpdir(), 'as-legacy-prune-'));
    return tempDir;
  };

  const writeFile = (path, content) => {
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, content, 'utf8');
  };

  const applyLegacyPrune = (basePath, overrides = {}) => {
    const effect = createLegacyPruneEffect();
    return effect.apply({
      basePath,
      legacyNamespaceDirs: ['.claude/commands'],
      namespaceName: 'atomic-skills',
      knownNames: new Set(['fix', 'historical-name']),
      ...overrides,
    });
  };

  it('preserves user files at legacy paths when their name is not known or frontmatter is absent', () => {
    const basePath = createTempDir();
    const unknownNamePath = join(basePath, '.claude/commands/atomic-skills/custom.md');
    const noFrontmatterPath = join(basePath, '.claude/commands/atomic-skills/plain.md');
    const unknownNameContent = '---\nname: custom-skill\n---\n# Mine\n';
    const noFrontmatterContent = '# Mine\n';
    writeFile(unknownNamePath, unknownNameContent);
    writeFile(noFrontmatterPath, noFrontmatterContent);

    const beforeState = applyLegacyPrune(basePath);

    assert.deepEqual(beforeState, { pruned: [] });
    assert.equal(readFileSync(unknownNamePath, 'utf8'), unknownNameContent);
    assert.equal(readFileSync(noFrontmatterPath, 'utf8'), noFrontmatterContent);
  });

  it('removes and records legacy files whose frontmatter name is known', () => {
    const basePath = createTempDir();
    const signedPath = join(basePath, '.claude/commands/atomic-skills/fix.md');
    const signedContent = '---\nname: "fix"\n---\n# Fix\n';
    writeFile(signedPath, signedContent);

    const beforeState = applyLegacyPrune(basePath);

    assert.equal(existsSync(signedPath), false);
    assert.deepEqual(beforeState, {
      pruned: [
        {
          path: '.claude/commands/atomic-skills/fix.md',
          content: signedContent,
        },
      ],
    });
  });

  it('preserves unreadable files, files without frontmatter, and files with unknown names', () => {
    const basePath = createTempDir();
    const unreadablePath = join(basePath, '.claude/commands/atomic-skills/unreadable.md');
    const noFrontmatterPath = join(basePath, '.claude/commands/atomic-skills/no-frontmatter.md');
    const unknownNamePath = join(basePath, '.claude/commands/atomic-skills/unknown.md');
    writeFile(unreadablePath, '---\nname: fix\n---\n# Would otherwise match\n');
    writeFile(noFrontmatterPath, 'not frontmatter\n---\nname: fix\n---\n');
    writeFile(unknownNamePath, '---\nname: unknown\n---\n# Unknown\n');
    chmodSync(unreadablePath, 0o000);

    const beforeState = applyLegacyPrune(basePath);

    assert.deepEqual(beforeState, { pruned: [] });
    assert.equal(existsSync(unreadablePath), true);
    assert.equal(readFileSync(noFrontmatterPath, 'utf8'), 'not frontmatter\n---\nname: fix\n---\n');
    assert.equal(readFileSync(unknownNamePath, 'utf8'), '---\nname: unknown\n---\n# Unknown\n');
    chmodSync(unreadablePath, 0o600);
  });

  it('throws on legacy dirs that escape basePath without touching outside files', () => {
    const root = createTempDir();
    const basePath = join(root, 'install');
    const outsidePath = join(root, 'legacy/atomic-skills/fix.md');
    mkdirSync(basePath, { recursive: true });
    writeFile(outsidePath, '---\nname: fix\n---\n# Outside\n');

    assert.throws(
      () => applyLegacyPrune(basePath, { legacyNamespaceDirs: ['../legacy'] }),
      /outside basePath/,
    );
    assert.equal(readFileSync(outsidePath, 'utf8'), '---\nname: fix\n---\n# Outside\n');
  });

  it('throws on pruned paths that escape basePath during revert without touching outside files', () => {
    const root = createTempDir();
    const basePath = join(root, 'install');
    const outsidePath = join(root, 'restored.md');
    mkdirSync(basePath, { recursive: true });
    const effect = createLegacyPruneEffect();

    assert.throws(
      () => effect.revert(
        { basePath },
        { pruned: [{ path: '../restored.md', content: 'outside\n' }] },
      ),
      /outside basePath/,
    );
    assert.equal(existsSync(outsidePath), false);
  });

  it('restores pruned files byte-for-byte on revert', () => {
    const basePath = createTempDir();
    const signedPath = join(basePath, '.claude/commands/atomic-skills/nested/fix.md');
    const signedContent = '---\nname: fix\n---\n# Fix\n\nExact bytes.\n';
    const effect = createLegacyPruneEffect();
    writeFile(signedPath, signedContent);

    const beforeState = effect.apply({
      basePath,
      legacyNamespaceDirs: ['.claude/commands'],
      namespaceName: 'atomic-skills',
      knownNames: new Set(['fix']),
    });
    effect.revert({ basePath }, beforeState);

    assert.equal(readFileSync(signedPath, 'utf8'), signedContent);
  });

  it('prunes empty parents through the namespace root while preserved siblings keep dirs intact', () => {
    const basePath = createTempDir();
    const onlySignedPath = join(basePath, '.claude/commands/atomic-skills/nested/dir/fix.md');
    const signedWithSiblingPath = join(basePath, '.gemini/skills/atomic-skills/nested/fix.md');
    const preservedSiblingPath = join(basePath, '.gemini/skills/atomic-skills/nested/custom.md');
    writeFile(onlySignedPath, '---\nname: fix\n---\n# Fix\n');
    writeFile(signedWithSiblingPath, '---\nname: fix\n---\n# Fix\n');
    writeFile(preservedSiblingPath, '---\nname: custom\n---\n# Mine\n');

    const beforeState = applyLegacyPrune(basePath, {
      legacyNamespaceDirs: ['.claude/commands', '.gemini/skills'],
    });

    assert.deepEqual(
      beforeState.pruned.map(({ path }) => path).sort(),
      [
        '.claude/commands/atomic-skills/nested/dir/fix.md',
        '.gemini/skills/atomic-skills/nested/fix.md',
      ],
    );
    assert.equal(existsSync(join(basePath, '.claude/commands/atomic-skills')), false);
    assert.equal(existsSync(join(basePath, '.claude/commands')), true);
    assert.equal(existsSync(join(basePath, '.gemini/skills/atomic-skills/nested')), true);
    assert.equal(readFileSync(preservedSiblingPath, 'utf8'), '---\nname: custom\n---\n# Mine\n');
  });

  it('skips missing legacy roots and treats empty revert state as a no-op', () => {
    const basePath = createTempDir();
    const effect = createLegacyPruneEffect();

    const beforeState = effect.apply({
      basePath,
      legacyNamespaceDirs: ['.missing/commands'],
      namespaceName: 'atomic-skills',
      knownNames: new Set(['fix']),
    });
    effect.revert({ basePath }, beforeState);
    effect.revert({ basePath }, { pruned: [] });

    assert.deepEqual(beforeState, { pruned: [] });
    assert.equal(existsSync(join(basePath, '.missing')), false);
  });
});

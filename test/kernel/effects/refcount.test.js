import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';

import { createRefcountEffect } from '../../../src/kernel/effects/refcount.js';
import { hashContent } from '../../../src/hash.js';

describe('refcount effect', () => {
  let tempDir;

  afterEach(() => {
    if (tempDir) {
      rmSync(tempDir, { recursive: true, force: true });
      tempDir = undefined;
    }
  });

  const createTempDir = () => {
    tempDir = mkdtempSync(join(tmpdir(), 'as-refcount-'));
    return tempDir;
  };

  const createManifest = (path) => {
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, '{}\n', 'utf8');
    return path;
  };

  const markerPath = (basePath, ownersDir, ownerId) => (
    join(basePath, ownersDir, hashContent(ownerId))
  );

  const listOwners = (basePath, ownersDir) => readdirSync(join(basePath, ownersDir)).sort();

  it('keeps a valid remaining owner when one of two installs is reverted', () => {
    const basePath = createTempDir();
    const ownersDir = '.atomic-skills/owners';
    const effect = createRefcountEffect();
    const ownerA = join(basePath, 'installs/a');
    const ownerB = join(basePath, 'installs/b');
    const manifestA = createManifest(join(ownerA, 'manifest.json'));
    const manifestB = createManifest(join(ownerB, 'manifest.json'));

    const stateA = effect.apply({
      basePath,
      ownersDir,
      ownerId: ownerA,
      ownerManifestPath: manifestA,
    });
    effect.apply({
      basePath,
      ownersDir,
      ownerId: ownerB,
      ownerManifestPath: manifestB,
    });

    const result = effect.revert({ basePath, ownersDir, ownerId: ownerA }, stateA);

    assert.equal(result.lastOwnerReleased, false);
    assert.equal(existsSync(markerPath(basePath, ownersDir, ownerA)), false);
    assert.equal(existsSync(markerPath(basePath, ownersDir, ownerB)), true);
    assert.deepEqual(listOwners(basePath, ownersDir), [hashContent(ownerB)]);
  });

  it('removes the owners dir and prunes empty parents after the last owner is reverted', () => {
    const basePath = createTempDir();
    const ownersDir = '.atomic-skills/owners';
    const effect = createRefcountEffect();
    const ownerA = join(basePath, 'installs/a');
    const ownerB = join(basePath, 'installs/b');
    const manifestA = createManifest(join(ownerA, 'manifest.json'));
    const manifestB = createManifest(join(ownerB, 'manifest.json'));

    const stateA = effect.apply({
      basePath,
      ownersDir,
      ownerId: ownerA,
      ownerManifestPath: manifestA,
    });
    const stateB = effect.apply({
      basePath,
      ownersDir,
      ownerId: ownerB,
      ownerManifestPath: manifestB,
    });

    effect.revert({ basePath, ownersDir, ownerId: ownerA }, stateA);
    const result = effect.revert({ basePath, ownersDir, ownerId: ownerB }, stateB);

    assert.deepEqual(result, { lastOwnerReleased: true });
    assert.equal(existsSync(join(basePath, ownersDir)), false);
    assert.equal(existsSync(join(basePath, '.atomic-skills')), false);
  });

  it('prunes orphan markers while reverting another owner', () => {
    const basePath = createTempDir();
    const ownersDir = '.atomic-skills/owners';
    const effect = createRefcountEffect();
    const liveOwner = join(basePath, 'installs/live');
    const liveManifest = createManifest(join(liveOwner, 'manifest.json'));
    const deadOwner = join(basePath, 'installs/dead');
    const deadManifest = join(deadOwner, 'manifest.json');
    const liveState = effect.apply({
      basePath,
      ownersDir,
      ownerId: liveOwner,
      ownerManifestPath: liveManifest,
    });
    const deadState = effect.apply({
      basePath,
      ownersDir,
      ownerId: deadOwner,
      ownerManifestPath: deadManifest,
    });

    const result = effect.revert({ basePath, ownersDir, ownerId: liveOwner }, liveState);

    assert.equal(result.lastOwnerReleased, true);
    assert.equal(existsSync(markerPath(basePath, ownersDir, liveOwner)), false);
    assert.equal(existsSync(markerPath(basePath, ownersDir, deadOwner)), false);
    assert.equal(deadState.markerExisted, false);
    assert.equal(existsSync(join(basePath, ownersDir)), false);
  });

  it('heals an empty owners dir left by a crash between release and reclaim', () => {
    const basePath = createTempDir();
    const ownersDir = '.atomic-skills/owners';
    const effect = createRefcountEffect();
    const owner = join(basePath, 'installs/owner');
    const manifest = createManifest(join(owner, 'manifest.json'));
    const state = effect.apply({
      basePath,
      ownersDir,
      ownerId: owner,
      ownerManifestPath: manifest,
    });
    unlinkSync(markerPath(basePath, ownersDir, owner));

    const result = effect.revert({ basePath, ownersDir, ownerId: owner }, state);

    assert.deepEqual(result, { lastOwnerReleased: true });
    assert.equal(existsSync(join(basePath, ownersDir)), false);
    assert.equal(existsSync(join(basePath, '.atomic-skills')), false);
  });

  it('heals an orphan marker left by a crash without touching a valid owner', () => {
    const basePath = createTempDir();
    const ownersDir = '.atomic-skills/owners';
    const effect = createRefcountEffect();
    const liveOwner = join(basePath, 'installs/live');
    const liveManifest = createManifest(join(liveOwner, 'manifest.json'));
    const deadOwner = join(basePath, 'installs/dead');
    const deadManifest = join(deadOwner, 'manifest.json');
    effect.apply({
      basePath,
      ownersDir,
      ownerId: liveOwner,
      ownerManifestPath: liveManifest,
    });
    const deadState = effect.apply({
      basePath,
      ownersDir,
      ownerId: deadOwner,
      ownerManifestPath: deadManifest,
    });
    unlinkSync(markerPath(basePath, ownersDir, deadOwner));
    mkdirSync(join(basePath, ownersDir), { recursive: true });
    writeFileSync(markerPath(basePath, ownersDir, deadOwner), `${deadManifest}\n`, 'utf8');

    const result = effect.revert({ basePath, ownersDir, ownerId: deadOwner }, deadState);

    assert.equal(result.lastOwnerReleased, false);
    assert.equal(existsSync(markerPath(basePath, ownersDir, deadOwner)), false);
    assert.equal(existsSync(markerPath(basePath, ownersDir, liveOwner)), true);
    assert.deepEqual(listOwners(basePath, ownersDir), [hashContent(liveOwner)]);
  });

  it('refuses ownersDir paths that escape basePath on apply and revert', () => {
    const root = createTempDir();
    const basePath = join(root, 'install');
    mkdirSync(basePath, { recursive: true });
    const escapeTarget = join(root, 'owners');
    const owner = join(basePath, 'owner');
    const manifest = createManifest(join(owner, 'manifest.json'));
    const ownerKey = hashContent(owner);
    const effect = createRefcountEffect();

    assert.throws(
      () => effect.apply({
        basePath,
        ownersDir: '../owners',
        ownerId: owner,
        ownerManifestPath: manifest,
      }),
      /outside basePath/,
    );
    assert.equal(existsSync(escapeTarget), false);

    assert.throws(
      () => effect.revert(
        { basePath, ownersDir: '../owners', ownerId: owner },
        { ownerKey, markerExisted: false, ownersDir: '../owners' },
      ),
      /outside basePath/,
    );
    assert.equal(existsSync(escapeTarget), false);
  });

  it('re-registers the same owner idempotently and reverts only the apply that created the claim', () => {
    const basePath = createTempDir();
    const ownersDir = '.atomic-skills/owners';
    const effect = createRefcountEffect();
    const owner = join(basePath, 'installs/owner');
    const manifest = createManifest(join(owner, 'manifest.json'));

    const firstState = effect.apply({
      basePath,
      ownersDir,
      ownerId: owner,
      ownerManifestPath: manifest,
    });
    const secondState = effect.apply({
      basePath,
      ownersDir,
      ownerId: owner,
      ownerManifestPath: manifest,
    });

    assert.equal(secondState.markerExisted, true);
    assert.deepEqual(listOwners(basePath, ownersDir), [hashContent(owner)]);

    const secondResult = effect.revert({ basePath, ownersDir, ownerId: owner }, secondState);

    assert.equal(secondResult.lastOwnerReleased, false);
    assert.equal(existsSync(markerPath(basePath, ownersDir, owner)), true);

    const firstResult = effect.revert({ basePath, ownersDir, ownerId: owner }, firstState);

    assert.deepEqual(firstResult, { lastOwnerReleased: true });
    assert.equal(existsSync(join(basePath, ownersDir)), false);
  });

  it('never removes a valid owner marker during another owner revert', () => {
    const basePath = createTempDir();
    const ownersDir = '.atomic-skills/owners';
    const effect = createRefcountEffect();
    const validOwner = join(basePath, 'installs/valid');
    const validManifest = createManifest(join(validOwner, 'manifest.json'));
    const releasingOwner = join(basePath, 'installs/releasing');
    const releasingManifest = createManifest(join(releasingOwner, 'manifest.json'));
    effect.apply({
      basePath,
      ownersDir,
      ownerId: validOwner,
      ownerManifestPath: validManifest,
    });
    const releasingState = effect.apply({
      basePath,
      ownersDir,
      ownerId: releasingOwner,
      ownerManifestPath: releasingManifest,
    });

    const result = effect.revert({ basePath, ownersDir, ownerId: releasingOwner }, releasingState);

    assert.equal(result.lastOwnerReleased, false);
    assert.equal(existsSync(markerPath(basePath, ownersDir, releasingOwner)), false);
    assert.equal(existsSync(markerPath(basePath, ownersDir, validOwner)), true);
    assert.equal(
      readFileSync(markerPath(basePath, ownersDir, validOwner), 'utf8'),
      `${validManifest}\n`,
    );
  });
});

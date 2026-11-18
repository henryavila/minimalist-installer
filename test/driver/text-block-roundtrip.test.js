import { describe, it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import { mkdtempSync, readFileSync, writeFileSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { defineInstaller } from '../../src/index.js';

describe('driver round-trip — built-in textBlock', () => {
  let root;
  afterEach(() => rmSync(root, { recursive: true, force: true }));
  for (const existing of [true, false]) {
    for (const update of [false, true]) {
      it(`round-trips existing=${existing}, update=${update}`, () => {
        root = mkdtempSync(join(tmpdir(), 'mi-text-driver-'));
        const file = join(root, 'instructions.md');
        const original = Buffer.from('user instructions\r\n');
        if (existing) writeFileSync(file, original);
        const make = (content) => defineInstaller({
          config: { manifestDir: '.mi', lockRoot: join(root, 'locks') },
          providers: [{ plan(_config, { basePath }) {
            return [{ type: 'textBlock', args: { basePath, path: 'instructions.md', id: 'x', content } }];
          } }],
        });
        let installer = make('first');
        installer.install({ projectDir: root });
        if (update) {
          installer = make('second');
          installer.install({ projectDir: root });
        }
        const installed = readFileSync(file, 'utf8');
        assert.ok(installed.includes(update ? 'second' : 'first'));
        assert.equal(installed.split('<!-- BEGIN x -->').length, 2);
        installer.uninstall({ projectDir: root });
        if (existing) assert.deepEqual(readFileSync(file), original);
        else assert.equal(existsSync(file), false);
      });
    }
  }

  const make = (blocks) => defineInstaller({
    config: { manifestDir: '.mi', lockRoot: join(root, 'locks') },
    providers: [{ plan(_config, { basePath }) {
      return blocks.map((args) => ({ type: 'textBlock', args: { basePath, content: 'ours', ...args } }));
    } }],
  });

  for (const existing of [true, false]) {
    it(`round-trips marker migration, existing=${existing}`, () => {
      root = mkdtempSync(join(tmpdir(), 'mi-text-driver-'));
      const file = join(root, 'a.md');
      const original = Buffer.from('instructions\r\n');
      if (existing) writeFileSync(file, original);
      make([{ path: 'a.md', id: 'x' }]).install({ projectDir: root });
      const next = make([{ path: 'a.md', id: 'x', markers: { begin: '# begin', end: '# end' } }]);
      next.install({ projectDir: root });
      assert.equal(readFileSync(file, 'utf8').includes('<!-- BEGIN x -->'), false);
      next.uninstall({ projectDir: root });
      if (existing) assert.deepEqual(readFileSync(file), original);
      else assert.equal(existsSync(file), false);
    });

    it(`round-trips two ids in one file, existing=${existing}`, () => {
      root = mkdtempSync(join(tmpdir(), 'mi-text-driver-'));
      const file = join(root, 'a.md');
      const original = Buffer.from('instructions\r\n');
      if (existing) writeFileSync(file, original);
      const installer = make([{ path: 'a.md', id: 'x' }, { path: 'a.md', id: 'y' }]);
      installer.install({ projectDir: root });
      assert.match(readFileSync(file, 'utf8'), /BEGIN x[\s\S]*BEGIN y/);
      installer.uninstall({ projectDir: root });
      if (existing) assert.deepEqual(readFileSync(file), original);
      else assert.equal(existsSync(file), false);
    });
  }

  it('does not inherit file ownership from the positional Driver fallback', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-text-driver-'));
    const b = join(root, 'b.md');
    const original = Buffer.from('<!-- BEGIN y -->\nexisting\n<!-- END y -->\n');
    writeFileSync(b, original);
    make([{ path: 'a.md', id: 'x' }]).install({ projectDir: root });
    const next = make([{ path: 'b.md', id: 'y' }, { path: 'a.md', id: 'x' }]);
    next.install({ projectDir: root });
    next.uninstall({ projectDir: root });
    assert.equal(existsSync(b), true);
    assert.deepEqual(readFileSync(b), original);
    assert.equal(existsSync(join(root, 'a.md')), false);
  });

  it('preserves a hand-edited block on uninstall', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-text-driver-'));
    const file = join(root, 'a.md');
    const installer = make([{ path: 'a.md', id: 'x' }]);
    installer.install({ projectDir: root });
    const edited = readFileSync(file, 'utf8').replace('ours', 'mine');
    writeFileSync(file, edited);
    installer.uninstall({ projectDir: root });
    assert.equal(readFileSync(file, 'utf8'), edited);
  });

  it('leaves an empty created file after reversing the order of two blocks', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-text-driver-'));
    const blocks = [{ path: 'a.md', id: 'x' }, { path: 'a.md', id: 'y' }];
    make(blocks).install({ projectDir: root });
    const next = make([...blocks].reverse());
    next.install({ projectDir: root });
    next.uninstall({ projectDir: root });
    // Known limitation: fileCreated belongs to x, which reverts before y after reordering.
    assert.deepEqual(readFileSync(join(root, 'a.md')), Buffer.alloc(0));
  });

  it('preserves surrounding bytes when a new block receives positional previous', () => {
    root = mkdtempSync(join(tmpdir(), 'mi-text-driver-'));
    const file = join(root, 'b.md');
    const original = Buffer.from('user instructions\r\n');
    writeFileSync(file, original);
    make([{ path: 'a.md', id: 'x' }]).install({ projectDir: root });
    const next = make([{ path: 'b.md', id: 'y' }, { path: 'a.md', id: 'x' }]);
    next.install({ projectDir: root });
    next.uninstall({ projectDir: root });
    assert.deepEqual(readFileSync(file), original);
    assert.equal(existsSync(join(root, 'a.md')), false);
  });

});

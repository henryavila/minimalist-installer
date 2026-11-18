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
});

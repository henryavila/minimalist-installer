import { it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import { mkdtempSync, readFileSync, writeFileSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { defineInstaller } from '../../src/index.js';

let root;
afterEach(() => { if (root) rmSync(root, { recursive: true, force: true }); });
const defaults = { begin: '<!-- BEGIN x -->', end: '<!-- END x -->' };
const custom = { begin: '# begin', end: '# end' };
const segment = (markers, content) => `${markers.begin}\n${content}\n${markers.end}\n`;
const setup = (original) => {
  root = mkdtempSync(join(tmpdir(), 'mi-lineage-'));
  if (original !== undefined) writeFileSync(join(root, 'a.md'), original);
};
const read = () => readFileSync(join(root, 'a.md'));
const install = (id = 'x', markers = defaults, content = 'ours') => {
  const installer = defineInstaller({
    config: { manifestDir: '.mi', lockRoot: join(root, 'locks') },
    providers: [{ plan(_config, { basePath }) {
      return [{ type: 'textBlock', args: { basePath, path: 'a.md', id, markers, content } }];
    } }],
  });
  installer.install({ projectDir: root });
  return installer;
};

const manifest = () => readFileSync(join(root, '.mi/manifest.json'));
const exact = (wanted) => assert.deepEqual(read(), Buffer.from(wanted));
const append = (s, b) => s + (!s || s.endsWith('\n\n') ? '' : s.endsWith('\n') ? '\n' : '\n\n') + b;
const refusal = (action) => {
  const bytes = read();
  const journal = manifest();
  assert.throws(action, /a\.md/);
  assert.deepEqual(read(), bytes);
  assert.deepEqual(manifest(), journal);
};
for (const adopted of [false, true]) {
  it(`R01 refuses migration onto an existing user block adopted=${adopted}`, () => {
    const original = segment(custom, 'target') + 'between\r\n' + (adopted ? segment(defaults, 'original x') : '') + 'tail\n';
    setup(original);
    const first = install();
    exact(adopted ? original.replace(segment(defaults, 'original x'), segment(defaults, 'ours')) : append(original, segment(defaults, 'ours')));
    refusal(() => install('x', custom));
    first.uninstall({ projectDir: root });
    exact(original);
  });
  it(`R02 refuses ownership transfer across ids adopted=${adopted}`, () => {
    setup(segment(defaults, 'original') + (adopted ? '' : 'tail'));
    install();
    refusal(() => install('y'));
  });
}
for (const initial of ['absent', 'plain', 'adopted']) {
  for (const markerChange of [false, true]) {
    for (const idChange of [false, true]) {
      for (const edit of ['none', 'outside', 'inside', 'broken', 'gone']) {
        it(`sequence initial=${initial} markers=${markerChange} id=${idChange} edit=${edit}`, () => {
          const original = initial === 'absent' ? undefined : 'head\r\n' + (initial === 'adopted' ? segment(defaults, 'original') : '') + 'tail\n';
          setup(original);
          const first = install();
          let current = initial === 'adopted' ? original.replace(segment(defaults, 'original'), segment(defaults, 'ours'))
            : append(original ?? '', segment(defaults, 'ours'));
          exact(current);
          let restored = original;
          if (edit === 'outside') { current += 'user suffix\r\n'; restored = (original ?? '') + 'user suffix\r\n'; }
          if (edit === 'inside') { current = current.replace('\nours\n', '\nhand edit\n'); restored = current; }
          if (edit === 'broken') { current = current.replace(defaults.begin, '# removed'); restored = current; }
          if (edit === 'gone') { current = 'user replacement'; restored = current; }
          writeFileSync(join(root, 'a.md'), current);
          exact(current);
          const target = markerChange ? custom : defaults;
          const id = idChange ? 'y' : 'x';
          if ((idChange && !markerChange && (initial === 'adopted' || edit === 'inside') && edit !== 'gone') || edit === 'broken') {
            refusal(() => install(id, target, 'second'));
            first.uninstall({ projectDir: root });
            if (restored === undefined) assert.equal(existsSync(join(root, 'a.md')), false);
            else exact(restored);
            return;
          }
          install(id, target, 'second');
          let expected;
          if (markerChange || idChange || edit === 'gone') expected = append(restored ?? '', segment(target, 'second'));
          else expected = edit === 'inside' ? current : current.replace('\nours\n', '\nsecond\n');
          exact(expected);
          const third = install(id, target, 'third');
          expected = edit === 'inside' && !markerChange ? expected : expected.replace('\nsecond\n', '\nthird\n');
          exact(expected);
          third.uninstall({ projectDir: root });
          if (restored === undefined) assert.equal(existsSync(join(root, 'a.md')), false);
          else exact(restored);
        });
      }
    }
  }
}
for (const reverse of [false, true]) {
  it(`refuses transfer from another journaled effect reverse=${reverse}`, () => {
    setup('head\n');
    const pair = (blocks) => defineInstaller({ config: { manifestDir: '.mi', lockRoot: join(root, 'locks') },
      providers: [{ plan(_c, { basePath }) { return blocks.map((args) => ({ type: 'textBlock', args: { basePath, path: 'a.md', content: 'ours', ...args } })); } }] });
    pair([{ id: 'x' }, { id: 'y' }]).install({ projectDir: root });
    const plan = [{ id: 'x', markers: { begin: '<!-- BEGIN y -->', end: '<!-- END y -->' } }, { id: 'y', markers: custom }];
    refusal(() => pair(reverse ? plan.reverse() : plan).install({ projectDir: root }));
  });
}
it('refuses content containing a removed journaled marker', () => {
  setup('head\n');
  install();
  refusal(() => install('y', custom, defaults.begin + '\nhello'));
});

for (const order of ['old-first', 'target-first']) {
  it(`refuses separate adoption migration with binary outside bytes order=${order}`, () => {
    const old = segment(defaults, 'original x').replaceAll('\n', '\r\n');
    const target = segment(custom, 'original target');
    const original = Buffer.concat([Buffer.from([0xff, 10]),
      Buffer.from(order === 'old-first' ? old + 'between\n' + target : target + 'between\n' + old), Buffer.from('tail')]);
    setup(original);
    const first = install();
    assert.deepEqual(read(), Buffer.concat([Buffer.from([0xff, 10]),
      Buffer.from(order === 'old-first' ? segment(defaults, 'ours') + 'between\n' + target : target + 'between\n' + segment(defaults, 'ours')), Buffer.from('tail')]));
    refusal(() => install('y', custom, 'second'));
    first.uninstall({ projectDir: root });
    assert.deepEqual(read(), original);
  });
}

it('generated id changes leave the adopted original in place on each fresh install', () => {
  const original = segment(defaults, 'original x');
  setup(original);
  install();
  exact(segment(defaults, 'ours'));
  install('y', null, 'second');
  exact(append(original, '<!-- BEGIN y -->\nsecond\n<!-- END y -->\n'));
  const next = install('z', null, 'third');
  exact(append(original, '<!-- BEGIN z -->\nthird\n<!-- END z -->\n'));
  next.uninstall({ projectDir: root });
  exact(original);
});

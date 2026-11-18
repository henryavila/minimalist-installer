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

for (const adopted of [false, true]) {
  it(`R01 restores both collision blocks in place, adopted=${adopted}`, () => {
    const original = Buffer.from(segment(custom, 'original target') + 'between\r\n'
      + (adopted ? segment(defaults, 'original x') : '') + 'tail\n');
    setup(original);
    install();
    const next = install('x', custom);
    next.uninstall({ projectDir: root });
    assert.deepEqual(read(), original);
  });
}
for (const adopted of [false, true]) {
  it(`R02 transfers original ownership across id changes, adopted=${adopted}`, () => {
    const original = adopted ? Buffer.from(segment(defaults, 'original x')) : undefined;
    setup(original);
    install();
    const next = install('y');
    next.uninstall({ projectDir: root });
    if (adopted) assert.deepEqual(read(), original);
    else assert.equal(existsSync(join(root, 'a.md')), false);
  });
}
it('R03 removes only the appended migration after a manual edit', () => {
  setup(segment(defaults, 'original x'));
  install();
  const edited = Buffer.from(segment(defaults, 'hand edit'));
  writeFileSync(join(root, 'a.md'), edited);
  const next = install('x', custom);
  assert.deepEqual(read(), Buffer.concat([edited, Buffer.from('\n' + segment(custom, 'ours'))]));
  next.uninstall({ projectDir: root });
  assert.deepEqual(read(), edited);
});

for (const initial of ['absent', 'plain', 'adopted']) {
  for (const markerChange of [false, true]) {
    for (const idChange of [false, true]) {
      for (const edit of ['none', 'outside', 'inside']) {
        it(`sequence initial=${initial} markers=${markerChange} id=${idChange} edit=${edit}`, () => {
          const original = initial === 'absent' ? undefined
            : Buffer.from('head\r\n' + (initial === 'adopted' ? segment(defaults, 'original') : '') + 'tail\n');
          setup(original);
          install();
          assert.ok(read().includes(Buffer.from(segment(defaults, 'ours'))));
          let expected = original;
          if (edit === 'outside') {
            writeFileSync(join(root, 'a.md'), Buffer.concat([read(), Buffer.from('user suffix\r\n')]));
            expected = Buffer.concat([original ?? Buffer.alloc(0), Buffer.from('user suffix\r\n')]);
          } else if (edit === 'inside') {
            writeFileSync(join(root, 'a.md'), read().toString().replace('ours', 'hand edit'));
            expected = read();
          }
          const markers = markerChange ? custom : defaults;
          const id = idChange ? 'y' : 'x';
          install(id, markers, 'second');
          assert.ok(read().includes(Buffer.from(segment(markers,
            edit === 'inside' && !markerChange ? 'hand edit' : 'second'))));
          const thirdMarkers = markerChange ? { begin: '# third begin', end: '# third end' } : markers;
          const next = install(idChange ? 'z' : id, thirdMarkers, 'third');
          assert.ok(read().includes(Buffer.from(segment(thirdMarkers,
            edit === 'inside' && !markerChange ? 'hand edit' : 'third'))));
          next.uninstall({ projectDir: root });
          if (expected === undefined) assert.equal(existsSync(join(root, 'a.md')), false);
          else assert.deepEqual(read(), expected);
        });
      }
    }
  }
}

for (const order of ['old-first', 'target-first']) {
  it(`restores separate adoptions through repeated collision migrations, ${order}`, () => {
    const old = segment(defaults, 'original x').replaceAll('\n', '\r\n');
    const target = segment(custom, 'original target');
    const original = Buffer.concat([Buffer.from([0xff, 10]),
      Buffer.from(order === 'old-first' ? old + 'between\n' + target : target + 'between\n' + old),
      Buffer.from('tail')]);
    setup(original);
    install();
    install('y', custom, 'second');
    const next = install('z', defaults, 'third');
    next.uninstall({ projectDir: root });
    assert.deepEqual(read(), original);
  });
}

it('refuses ambiguous id and marker migrations without changing target bytes', () => {
  setup('user\n');
  const installer = defineInstaller({
    config: { manifestDir: '.mi', lockRoot: join(root, 'locks') },
    providers: [{ plan(_config, { basePath }) {
      return ['x', 'y'].map((id) => ({ type: 'textBlock',
        args: { basePath, path: 'a.md', id, content: 'ours' } }));
    } }],
  });
  installer.install({ projectDir: root });
  const before = read();
  assert.throws(() => install('z', custom), /Ambiguous textBlock identity/);
  assert.deepEqual(read(), before);
});

it('does not adopt another journaled effect as original user bytes', () => {
  setup('user\n');
  const pair = (collision) => defineInstaller({
    config: { manifestDir: '.mi', lockRoot: join(root, 'locks') },
    providers: [{ plan(_config, { basePath }) {
      return (collision ? ['x'] : ['x', 'y']).map((id) => ({ type: 'textBlock', args: {
        basePath, path: 'a.md', id, content: 'ours',
        ...(collision && id === 'x' ? { markers: { begin: '<!-- BEGIN y -->', end: '<!-- END y -->' } } : {}),
      } }));
    } }],
  });
  pair(false).install({ projectDir: root });
  const before = read();
  assert.throws(() => pair(true).install({ projectDir: root }), /textBlock.*collision/i);
  assert.deepEqual(read(), before);
});

it('carries adoption through id changes with generated default markers', () => {
  const original = Buffer.from(segment(defaults, 'original x'));
  setup(original);
  install();
  install('y', null, 'second');
  const next = install('z', null, 'third');
  next.uninstall({ projectDir: root });
  assert.deepEqual(read(), original);
});

it('rejects two planned ids sharing markers before writing either block', () => {
  const original = Buffer.from('user\n');
  setup(original);
  const installer = defineInstaller({
    config: { manifestDir: '.mi', lockRoot: join(root, 'locks') },
    providers: [{ plan(_config, { basePath }) {
      return ['x', 'y'].map((id) => ({ type: 'textBlock',
        args: { basePath, path: 'a.md', id, markers: custom, content: 'ours' } }));
    } }],
  });
  assert.throws(() => installer.install({ projectDir: root }), /textBlock.*collision/i);
  assert.deepEqual(read(), original);
});

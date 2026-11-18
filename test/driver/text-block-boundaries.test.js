import { it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { hashContent } from '../../src/hash.js';
import { defineInstaller } from '../../src/index.js';

let root;
afterEach(() => { if (root) rmSync(root, { recursive: true, force: true }); });
const markers = (id) => ({ begin: `<!-- BEGIN ${id} -->`, end: `<!-- END ${id} -->` });
const block = (id, content, m = markers(id)) => `${m.begin}\n${content}\n${m.end}\n`;
const setup = (original) => {
  root = mkdtempSync(join(tmpdir(), 'mi-boundaries-'));
  writeFileSync(join(root, 'a.md'), original);
};
const read = () => readFileSync(join(root, 'a.md'));
const journal = () => readFileSync(join(root, '.mi/manifest.json'));
const make = (blocks) => defineInstaller({
  config: { manifestDir: '.mi', lockRoot: join(root, 'locks') },
  providers: [{ plan(_config, { basePath }) {
    return blocks.map(({ plannedId, ...args }) => ({ type: 'textBlock', ...(plannedId ? { id: plannedId } : {}), args: { basePath, path: 'a.md', content: 'ours', ...args } }));
  } }],
});
const run = (blocks) => {
  const installer = make(blocks);
  installer.install({ projectDir: root });
  return installer;
};

for (const first of [['y'], ['y', 'x']]) {
  it(`R4-R01 adjacent separator ownership first=${first}`, () => {
    const original = Buffer.from(block('x', 'original').replaceAll('\n', '\r\n').trimEnd());
    setup(original);
    run(first.map((id) => ({ id })));
    assert.equal(read().toString(), (first.includes('x') ? block('x', 'ours').trimEnd() : original.toString())
      + '\n\n' + block('y', 'ours'));
    const next = run([{ id: 'x', content: 'v2x' }, { id: 'y', content: 'v2y' }]);
    assert.match(read().toString(), /v2x/);
    assert.match(read().toString(), /v2y/);
    next.uninstall({ projectDir: root });
    assert.deepEqual(read(), original);
  });
}
for (const adopted of [false, true]) {
  it(`R4-R02 migrates onto user-added markers without joining lines adopted=${adopted}`, () => {
    const original = adopted ? block('x', 'original').trimEnd() : 'head';
    setup(original);
    run([{ id: 'x' }]);
    assert.equal(read().toString(), adopted ? block('x', 'ours') : original + '\n\n' + block('x', 'ours'));
    const target = { begin: '# begin', end: '# end' };
    writeFileSync(join(root, 'a.md'), Buffer.concat([read(), Buffer.from(block('x', 'user block', target))]));
    const next = run([{ id: 'x', markers: target, content: 'updated' }]);
    assert.equal(read().toString(), original + '\n' + block('x', 'updated', target));
    const third = run([{ id: 'x', markers: markers('third'), content: 'third update' }]);
    assert.equal(read().toString(), original + '\n' + block('third', 'third update'));
    third.uninstall({ projectDir: root });
    assert.equal(read().toString(), original + block('x', 'user block', target));
  });
}
for (const reverse of [false, true]) {
  for (const invalid of ['collision', 'ambiguous', 'malformed', 'shared-marker']) {
    it(`R4-R03 validates entire plan before mutation ${invalid} reverse=${reverse}`, () => {
      setup('head\n');
      run([{ id: 'x' }, { id: 'y' }]);
      if (invalid === 'malformed') writeFileSync(join(root, 'a.md'), Buffer.concat([read(), Buffer.from('# broken\n')]));
      const bytes = read();
      const manifest = journal();
      let plan;
      if (invalid === 'collision') plan = [{ id: 'x', markers: markers('z') }, { id: 'y', markers: markers('x') }];
      if (invalid === 'ambiguous') plan = [{ id: 'a', markers: markers('a') }, { id: 'b', markers: markers('b') }];
      if (invalid === 'malformed') plan = [{ id: 'x', content: 'updated' }, { id: 'y', markers: { begin: '# broken', end: '# missing' } }];
      if (invalid === 'shared-marker') plan = [{ id: 'x' }, { id: 'y', markers: { begin: markers('x').end, end: '# other' } }];
      if (reverse) plan.reverse();
      assert.throws(() => run(plan), /collision|Ambiguous|Malformed/);
      assert.deepEqual(read(), bytes);
      assert.deepEqual(journal(), manifest);
    });
  }
  it(`R4-R03 resolves marker matches before unmatched renames reverse=${reverse}`, () => {
    setup('head\n');
    run([{ id: 'x' }, { id: 'y' }]);
    const plan = [{ id: 'b', markers: markers('z'), content: 'second b' }, { id: 'a', markers: markers('y'), content: 'second a' }];
    if (reverse) plan.reverse();
    const next = run(plan);
    assert.equal(read().toString(), 'head\n\n' + block('z', 'second b') + '\n' + block('y', 'second a'));
    next.uninstall({ projectDir: root });
    assert.equal(read().toString(), 'head\n');
  });
}
it('R4-R03 rejected greenfield plan leaves no manifest', () => {
  setup('head');
  assert.throws(() => run([{ id: 'x' }, { id: 'y', markers: markers('x') }]), /collision/);
  assert.equal(read().toString(), 'head');
  assert.equal(existsSync(join(root, '.mi/manifest.json')), false);
});

for (const ending of ['', '\n', '\r\n']) {
  for (const adopted of [false, true]) {
    for (const reverse of [false, true]) {
      for (const outside of [false, true]) {
        it(`R4-R04 sequence boundaries ending=${JSON.stringify(ending)} adopted=${adopted} reverse=${reverse} outside=${outside}`, () => {
          const eol = ending === '\r\n' ? ending : '\n';
          const original = adopted
            ? 'head' + eol + block('x', 'original').trimEnd().replaceAll('\n', eol) + ending
            : 'head' + ending;
          setup(original);
          const order = reverse ? ['y', 'x'] : ['x', 'y'];
          const suffix = outside ? 'user suffix\r\n' : '';
          const expected = (stage) => {
            const segmentFor = (id) => block(`${id}${stage}`, `${stage}-${id}`);
            if (adopted) return 'head' + eol + segmentFor('x') + '\n' + segmentFor('y') + suffix;
            const spacing = ending ? '\n' : '\n\n';
            return original + spacing + order.map(segmentFor).join('\n') + suffix;
          };
          let next;
          for (let stage = 1; stage <= 3; stage++) {
            const ids = stage === 2 ? [...order].reverse() : order;
            next = run(ids.map((id) => ({ id, markers: stage === 1 && adopted && id === 'x'
              ? markers('x') : markers(`${id}${stage}`), content: `${stage}-${id}` })));
            // Stage one adopts the original markers; stages two and three each migrate.
            const wanted = stage === 1 && adopted
              ? expected(stage).replaceAll('BEGIN x1', 'BEGIN x').replaceAll('END x1', 'END x')
              : expected(stage);
            if (stage === 1 && outside) {
              assert.equal(read().toString(), wanted.slice(0, -suffix.length));
              writeFileSync(join(root, 'a.md'), Buffer.concat([read(), Buffer.from(suffix)]));
            }
            assert.equal(read().toString(), wanted, `after install ${stage}`);
          }
          next.uninstall({ projectDir: root });
          assert.equal(read().toString(), original + suffix);
        });
      }
    }
  }
}

for (const when of ['before-update', 'after-update']) {
  it(`R4-R04 respects file deletion ${when}`, () => {
    setup('head');
    run([{ id: 'x' }]);
    assert.equal(read().toString(), 'head\n\n' + block('x', 'ours'));
    if (when === 'before-update') rmSync(join(root, 'a.md'));
    const next = run([{ id: 'x', markers: markers('z'), content: 'updated' }]);
    assert.equal(read().toString(), (when === 'before-update' ? '' : 'head\n\n') + block('z', 'updated'));
    rmSync(join(root, 'a.md'));
    next.uninstall({ projectDir: root });
    assert.equal(existsSync(join(root, 'a.md')), false);
  });
}

for (const adopted of [false, true]) {
  for (const update of [false, true]) {
    it(`R4-R04 reads 0e12991 journal adopted=${adopted} update=${update}`, () => {
      const original = adopted ? block('x', 'original').replaceAll('\n', '\r\n') : 'head';
      setup(original);
      const installed = adopted ? block('x', 'ours') : original + '\n\n' + block('x', 'ours');
      writeFileSync(join(root, 'a.md'), installed);
      // Exact before-state fields produced by 0e12991, without newer metadata.
      const state = { path: 'a.md', id: 'x', ...markers('x'), fileCreated: false,
        prefix: adopted ? '' : '\n\n', segmentHash: hashContent(block('x', 'ours')),
        ...(adopted ? { replaced: Buffer.from(original).toString('base64') } : {}) };
      mkdirSync(join(root, '.mi'));
      writeFileSync(join(root, '.mi/manifest.json'), JSON.stringify({ journalVersion: 2,
        effects: [{ type: 'textBlock', id: 'textBlock:a.md#x', beforeState: state }],
        transaction: { state: 'complete' } }));
      assert.equal(read().toString(), installed);
      const next = update ? run([{ id: 'x', markers: markers('z'), content: 'updated' }]) : make([]);
      if (update) assert.equal(read().toString(), (adopted ? '' : original + '\n\n') + block('z', 'updated'));
      next.uninstall({ projectDir: root });
      assert.equal(read().toString(), original);
    });
  }
}

it('R4-R05 honors explicit planned ids during simultaneous migrations', () => {
  setup('head');
  run([{ id: 'x', plannedId: 'left' }, { id: 'y', plannedId: 'right' }]);
  assert.equal(read().toString(), 'head\n\n' + block('x', 'ours') + '\n' + block('y', 'ours'));
  const next = run([{ id: 'b', plannedId: 'left', markers: markers('z'), content: 'left update' },
    { id: 'a', plannedId: 'right', markers: markers('w'), content: 'right update' }]);
  assert.equal(read().toString(), 'head\n\n' + block('z', 'left update') + '\n' + block('w', 'right update'));
  const manifest = JSON.parse(journal());
  assert.deepEqual(manifest.effects.map(({ id }) => id), ['left', 'right']);
  next.uninstall({ projectDir: root });
  assert.equal(read().toString(), 'head');
});

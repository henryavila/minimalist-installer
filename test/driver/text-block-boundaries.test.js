import { it, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync, rmSync, existsSync, symlinkSync } from 'node:fs';
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


const exact = (wanted) => assert.deepEqual(read(), Buffer.from(wanted));
const append = (bytes, segment) => bytes + (!bytes || bytes.endsWith('\n\n') ? '' : bytes.endsWith('\n') ? '\n' : '\n\n') + segment;
const permutations = (xs) => xs.length ? xs.flatMap((x, i) => permutations(xs.filter((_, j) => i !== j)).map((rest) => [x, ...rest])) : [[]];
const refuse = (plan, reason = /a\.md/) => {
  const bytes = read();
  const manifest = existsSync(join(root, '.mi/manifest.json')) ? journal() : null;
  assert.throws(() => run(plan), reason);
  assert.deepEqual(read(), bytes);
  if (manifest) assert.deepEqual(journal(), manifest);
  else assert.equal(existsSync(join(root, '.mi/manifest.json')), false);
};

for (const originalHead of ['', 'head', 'head\n', 'head\r\n']) {
  for (const adoption of ['none', 'EOF', 'LF', 'CRLF']) {
    for (const count of [1, 2, 3]) {
      for (const order of permutations(['x', 'y', 'z'].slice(0, count))) {
        it(`contract matrix head=${JSON.stringify(originalHead)} adoption=${adoption} order=${order}`, () => {
          const old = adoption === 'none' ? '' : block('x', 'original').trimEnd().replaceAll('\n', adoption === 'CRLF' ? '\r\n' : '\n')
            + (adoption === 'EOF' ? '' : adoption === 'CRLF' ? '\r\n' : '\n');
          const original = originalHead + (old && originalHead && !originalHead.endsWith('\n') ? '\n' : '') + old;
          setup(original);
          let expected = original;
          for (const id of order) {
            if (id === 'x' && old) {
              expected = expected.replace(old, block('x', 'ours'));
            } else expected = append(expected, block(id, 'ours'));
          }
          run(order.map((id) => ({ id })));
          exact(expected);
          const suffix = '# begin EXTRA\nuser suffix\r\n';
          writeFileSync(join(root, 'a.md'), Buffer.concat([read(), Buffer.from(suffix)]));
          expected += suffix;
          exact(expected);
          const second = run([...order].reverse().map((id) => ({ id, content: 'updated' })));
          expected = expected.replaceAll('\nours\n', '\nupdated\n');
          exact(expected);
          second.uninstall({ projectDir: root });
          exact(original + suffix);
          // Start the independent identity-change sequence from the original bytes.
          rmSync(root, { recursive: true, force: true });
          setup(original);
          run(order.map((id) => ({ id })));
          let installed = original;
          for (const id of order) {
            if (id === 'x' && old) installed = installed.replace(old,
              block('x', 'ours'));
            else installed = append(installed, block(id, 'ours'));
          }
          exact(installed);
          writeFileSync(join(root, 'a.md'), Buffer.concat([read(), Buffer.from(suffix)]));
          exact(installed + suffix);
          const fresh = [...order].reverse().map((id) => ({ id: id + '2', markers: markers(id + '2'), content: 'fresh' }));
          const next = run(fresh);
          let migrated = original + suffix;
          for (const { id } of fresh) migrated = append(migrated, block(id, 'fresh'));
          exact(migrated);
          next.uninstall({ projectDir: root });
          exact(original + suffix);
        });
      }
    }
  }
}

for (const probe of ['P1', 'P2', 'P3', 'P4', 'P7']) {
  for (const reverse of [false, true]) {
    it(`R5-R01/R02 ${probe} refuses a user-added migration target reverse=${reverse}`, () => {
      const adopted = ['P2', 'P7'].includes(probe);
      const original = adopted ? block('x', 'original').replaceAll('\n', '\r\n').trimEnd() : 'head';
      setup(original);
      let plan = adopted ? [{ id: 'y' }] : [{ id: 'x' }, { id: 'y' }];
      if (reverse) plan.reverse();
      run(plan);
      exact(adopted ? append(original, block('y', 'ours')) : plan.reduce((s, { id }) => append(s, block(id, 'ours')), original));
      if (adopted) {
        run([{ id: 'x' }, { id: 'y' }]);
        exact(block('x', 'ours') + '\n\n' + block('y', 'ours'));
      }
      const target = { begin: '# begin', end: '# end' };
      const extra = (probe === 'P4' ? '# begin EXTRA\n' : '') + block('target', 'user block', target);
      writeFileSync(join(root, 'a.md'), Buffer.concat([read(), Buffer.from(extra)]));
      const changed = probe === 'P2' ? 'y' : 'x';
      plan = ['x', 'y'].map((id) => ({ id, content: 'updated', ...(id === changed ? { markers: target } : {}) }));
      if (reverse) plan.reverse();
      refuse(plan);
      make([]).uninstall({ projectDir: root });
      exact(original + extra);
    });
  }
}
for (const reverse of [false, true]) {
  it(`R5-R03 P5 reserves explicit planned ids first reverse=${reverse}`, () => {
    setup('head');
    run([{ plannedId: 'left', id: 'x' }]);
    exact(append('head', block('x', 'ours')));
    const plan = [{ plannedId: 'brand-new', id: 'x', markers: markers('z'), content: 'new' },
      { plannedId: 'left', id: 'y', content: 'updated' }];
    if (reverse) plan.reverse();
    const next = run(plan);
    exact(plan.reduce((s, a) => append(s, block(a.id, a.content, a.markers)), 'head'));
    next.uninstall({ projectDir: root });
    exact('head');
  });
  for (const marker of ['begin', 'end']) {
    it(`R5-R04 P10 cross-content ${marker} reverse=${reverse}`, () => {
      setup('head\n');
      const plan = [{ id: 'x', content: markers('y')[marker] + '\nhello' }, { id: 'y', content: 'world' }];
      refuse(reverse ? plan.reverse() : plan);
    });
  }
  for (const greenfield of [false, true]) {
    for (const invalid of ['collision', 'same-id', 'shared-marker', 'dup-plannedId', 'bad-id', 'bad-content-type',
      'bad-content-marker', 'bad-markers-same', 'bad-markers-empty', 'malformed-old', 'malformed-newer', 'duplicate', 'nested', 'path-escape']) {
      it(`R5-R05 P9 validates whole plan ${invalid} greenfield=${greenfield} reverse=${reverse}`, () => {
        setup('head\n');
        if (!greenfield) run([{ id: 'x' }, { id: 'y' }]);
        let bad = { id: 'z' };
        let good = { id: 'x', content: 'updated' };
        if (invalid === 'collision') bad.markers = markers('x');
        if (invalid === 'same-id') bad.id = 'x';
        if (invalid === 'shared-marker') bad.markers = { begin: markers('x').end, end: '# other' };
        if (invalid === 'dup-plannedId') { good.plannedId = 'same'; bad.plannedId = 'same'; }
        if (invalid === 'bad-id') bad.id = 'a b';
        if (invalid === 'bad-content-type') bad.content = 42;
        if (invalid === 'bad-content-marker') bad.content = markers('x').begin;
        if (invalid === 'bad-markers-same') bad.markers = { begin: '# same', end: '# same' };
        if (invalid === 'bad-markers-empty') bad.markers = { begin: '', end: '# end' };
        if (invalid === 'path-escape') bad.path = '../escape.md';
        if (invalid === 'malformed-old') {
          writeFileSync(join(root, 'a.md'), greenfield ? markers('x').begin + '\n' : read().toString().replace(markers('x').end, '# broken'));
        }
        if (invalid === 'malformed-newer') writeFileSync(join(root, 'a.md'), Buffer.concat([read(), Buffer.from(markers('z').begin + '\n')]));
        if (invalid === 'duplicate') writeFileSync(join(root, 'a.md'), Buffer.concat([read(), Buffer.from(block('z', 'one') + block('z', 'two'))]));
        if (invalid === 'nested') {
          writeFileSync(join(root, 'a.md'), block('x', block('z', 'nested')));
        }
        const plan = [good, bad];
        if (reverse) plan.reverse();
        const installer = make(plan);
        const before = read();
        const manifest = greenfield ? null : journal();
        assert.throws(() => installer.install({ projectDir: root }), invalid === 'path-escape' ? /outside basePath/ : /a\.md/);
        assert.deepEqual(read(), before);
        if (manifest) assert.deepEqual(journal(), manifest);
        else assert.equal(existsSync(join(root, '.mi/manifest.json')), false);
      });
    }
  }
}

for (const adopted of [false, true]) {
  for (const update of [false, true]) {
    it(`R5 P8 legacy 0e12991 exact adopted=${adopted} update=${update}`, () => {
      const original = adopted ? block('x', 'original').replaceAll('\n', '\r\n') : 'head';
      setup(original);
      const installed = adopted ? block('x', 'ours') : append(original, block('x', 'ours'));
      writeFileSync(join(root, 'a.md'), installed);
      const state = { path: 'a.md', id: 'x', ...markers('x'), fileCreated: false,
        prefix: adopted ? '' : '\n\n', segmentHash: hashContent(block('x', 'ours')),
        ...(adopted ? { replaced: Buffer.from(original).toString('base64') } : {}) };
      mkdirSync(join(root, '.mi'));
      writeFileSync(join(root, '.mi/manifest.json'), JSON.stringify({ journalVersion: 2,
        effects: [{ type: 'textBlock', id: 'textBlock:a.md#x', beforeState: state }], transaction: { state: 'complete' } }));
      exact(installed);
      const next = update ? run([{ id: 'x', markers: markers('z'), content: 'updated' }]) : make([]);
      if (update) exact(append(original, block('z', 'updated')));
      next.uninstall({ projectDir: root });
      exact(original);
    });
  }
}

for (const order of permutations(['x', 'y', 'z'])) {
  for (const adopted of [false, true]) {
    it(`R5 P6 partial replacement preserves peers order=${order} adopted=${adopted}`, () => {
      const original = adopted ? block('x', 'original').replaceAll('\n', '\r\n').trimEnd() : 'head';
      setup(original);
      run(order.map((id) => ({ id })));
      // Describe the installed bytes and the exact substitution owned by x.
      let initial = original;
      let xInserted;
      let xReplaced = '';
      for (const id of order) {
        if (id === 'x' && adopted) {
          xInserted = block('x', 'ours');
          xReplaced = original;
          initial = initial.replace(original, xInserted);
        } else {
          const next = append(initial, block(id, 'ours'));
          if (id === 'x') xInserted = next.slice(initial.length);
          initial = next;
        }
      }
      exact(initial);
      const plan = [...order].reverse().map((id) => ({ id, content: 'second', ...(id === 'x' ? { markers: markers('new') } : {}) }));
      const next = run(plan);
      const withoutX = initial.replace(xInserted, xReplaced).replaceAll('\nours\n', '\nsecond\n');
      exact(append(withoutX, block('new', 'second')));
      next.uninstall({ projectDir: root });
      exact(original);
    });
  }
}

it('R5 P4 prefix-only target line survives accepted replacement exactly', () => {
  setup('head');
  run([{ id: 'x' }]);
  exact(append('head', block('x', 'ours')));
  writeFileSync(join(root, 'a.md'), Buffer.concat([read(), Buffer.from('# begin EXTRA\n')]));
  exact(append('head', block('x', 'ours')) + '# begin EXTRA\n');
  const next = run([{ id: 'x', markers: { begin: '# begin', end: '# end' }, content: 'updated' }]);
  exact(append('head# begin EXTRA\n', '# begin\nupdated\n# end\n'));
  next.uninstall({ projectDir: root });
  exact('head# begin EXTRA\n');
});

for (const when of ['before-update', 'after-update']) {
  it(`respects file deletion ${when}`, () => {
    setup('head');
    run([{ id: 'x' }]);
    exact(append('head', block('x', 'ours')));
    if (when === 'before-update') rmSync(join(root, 'a.md'));
    const next = run([{ id: 'x', markers: markers('z'), content: 'updated' }]);
    exact(append(when === 'before-update' ? '' : 'head', block('z', 'updated')));
    rmSync(join(root, 'a.md'));
    next.uninstall({ projectDir: root });
    assert.equal(existsSync(join(root, 'a.md')), false);
  });
}

for (const reverse of [false, true]) {
  it(`explicit planned ids change both identities reverse=${reverse}`, () => {
    setup('head');
    run([{ id: 'x', plannedId: 'left' }, { id: 'y', plannedId: 'right' }]);
    exact(append(append('head', block('x', 'ours')), block('y', 'ours')));
    const plan = [{ id: 'b', plannedId: 'left', markers: markers('z'), content: 'left update' },
      { id: 'a', plannedId: 'right', markers: markers('w'), content: 'right update' }];
    if (reverse) plan.reverse();
    const next = run(plan);
    exact(plan.reduce((s, a) => append(s, block(a.id, a.content, a.markers)), 'head'));
    assert.deepEqual(JSON.parse(journal()).effects.map(({ id }) => id), plan.map(({ plannedId }) => plannedId));
    next.uninstall({ projectDir: root });
    exact('head');
  });
}

it('whole lines preserve a lone CR at EOF as user text', () => {
  const original = markers('x').begin + '\r';
  setup(original);
  refuse([{ id: 'x' }]);
  exact(original);
});

for (const order of permutations(['x', 'y', 'z'])) {
  it(`created file exact through reordering and replacement order=${order}`, () => {
    setup('');
    rmSync(join(root, 'a.md'));
    run(order.map((id) => ({ id })));
    const first = order.reduce((s, id) => append(s, block(id, 'ours')), '');
    exact(first);
    run([...order].reverse().map((id) => ({ id, content: 'second' })));
    exact(first.replaceAll('\nours\n', '\nsecond\n'));
    const fresh = [...order].reverse().map((id) => ({ id: id + '2', content: 'third' }));
    const next = run(fresh);
    exact(fresh.reduce((s, { id }) => append(s, block(id, 'third')), ''));
    next.uninstall({ projectDir: root });
    assert.equal(existsSync(join(root, 'a.md')), false);
  });
}

for (const reverse of [false, true]) {
  it(`R5 P9 refusal preserves coplanned jsonMerge and custom effects reverse=${reverse}`, () => {
    setup('head\n');
    run([{ id: 'x' }]);
    writeFileSync(join(root, 'config.json'), '{ "user": true }\n');
    const target = read();
    const before = journal();
    const json = readFileSync(join(root, 'config.json'));
    let applied = false;
    const installer = defineInstaller({
      config: { manifestDir: '.mi', lockRoot: join(root, 'locks') },
      effects: [{ type: 'probe', apply() { applied = true; }, revert() {} }],
      providers: [{ plan(_c, { basePath }) {
        const plan = [
          { type: 'probe', args: {} },
          { type: 'jsonMerge', args: { basePath, path: 'config.json', delta: { installed: true } } },
          { type: 'textBlock', args: { basePath, path: 'a.md', id: 'x', content: 'updated' } },
          { type: 'textBlock', args: { basePath, path: 'a.md', id: 'y', content: markers('x').begin } },
        ];
        return reverse ? plan.reverse() : plan;
      } }],
    });
    assert.throws(() => installer.install({ projectDir: root }), /a\.md/);
    assert.deepEqual(read(), target);
    assert.deepEqual(journal(), before);
    assert.deepEqual(readFileSync(join(root, 'config.json')), json);
    assert.equal(applied, false);
  });
}

for (const original of [undefined, 'head', block('x', 'original').replaceAll('\n', '\r\n').trimEnd()]) {
  for (const update of [false, true]) {
    it(`R5 P8 legacy two-block journal original=${JSON.stringify(original)} update=${update}`, () => {
      setup(original ?? '');
      const adopted = original?.includes('BEGIN');
      const x = block('x', 'ours');
      const installed = append(adopted ? x : append(original ?? '', x), block('y', 'ours'));
      writeFileSync(join(root, 'a.md'), installed);
      const states = [
        { path: 'a.md', id: 'x', ...markers('x'), fileCreated: original === undefined,
          prefix: adopted || original === undefined ? '' : '\n\n', segmentHash: hashContent(x),
          ...(adopted ? { replaced: Buffer.from(original).toString('base64') } : {}) },
        { path: 'a.md', id: 'y', ...markers('y'), fileCreated: false, prefix: '\n', segmentHash: hashContent(block('y', 'ours')) },
      ];
      mkdirSync(join(root, '.mi'));
      writeFileSync(join(root, '.mi/manifest.json'), JSON.stringify({ journalVersion: 2,
        effects: states.map((beforeState) => ({ type: 'textBlock', id: `textBlock:a.md#${beforeState.id}`, beforeState })),
        transaction: { state: 'complete' } }));
      exact(installed);
      const next = update ? run([{ id: 'y', content: 'second' }, { id: 'x', content: 'second' }]) : make([]);
      if (update) exact(installed.replaceAll('\nours\n', '\nsecond\n'));
      next.uninstall({ projectDir: root });
      if (original === undefined) assert.equal(existsSync(join(root, 'a.md')), false);
      else exact(original);
    });
  }
}

for (const reverse of [false, true]) {
  it(`single marker change with retained peer reverse=${reverse}`, () => {
    setup('head');
    run([{ id: 'x' }, { id: 'y' }]);
    exact(append(append('head', block('x', 'ours')), block('y', 'ours')));
    const changed = { begin: '# new begin', end: markers('x').end };
    const plan = [{ id: 'x', markers: changed }, { id: 'y' }];
    const next = run(reverse ? plan.reverse() : plan);
    exact(append('head\n' + block('y', 'ours'), block('x', 'ours', changed)));
    next.uninstall({ projectDir: root });
    exact('head');
  });
}

for (const reverse of [false, true]) {
  it(`R5 P9 refuses a symlink path before mutation reverse=${reverse}`, () => {
    setup('head');
    run([{ id: 'x' }]);
    const before = journal();
    const bytes = read();
    symlinkSync(join(root, 'a.md'), join(root, 'linked.md'));
    const plan = [{ id: 'x', content: 'updated' }, { id: 'y', path: 'linked.md' }];
    assert.throws(() => run(reverse ? plan.reverse() : plan), /symlink/i);
    assert.deepEqual(read(), bytes);
    assert.deepEqual(journal(), before);
  });
}

for (const eol of ['\n', '\r\n']) {
  for (const mode of ['recreated-append', 'removed-append', 'recreated-control']) {
    it(`R6 T1 independent end-marker terminator ${mode} eol=${JSON.stringify(eol)}`, () => {
      const original = block('x', 'original').trimEnd().replaceAll('\n', eol);
      setup(original);
      run([{ id: 'y', content: 'y1' }, { id: 'x' }]);
      exact(block('x', 'ours') + '\n\n' + block('y', 'y1'));
      const states = JSON.parse(journal()).effects.map(({ beforeState }) => beforeState);
      assert.deepEqual(Buffer.from(states.find((s) => s.id === 'x').inserted, 'base64'), Buffer.from(block('x', 'ours')));
      assert.deepEqual(Buffer.from(states.find((s) => s.id === 'x').replaced, 'base64'), Buffer.from(original));
      const plan = [{ id: 'x' }];
      if (mode !== 'removed-append') plan.unshift({ id: 'y', markers: { begin: '# begin y', end: '# end y' }, content: 'y2' });
      const next = run(plan);
      exact(mode === 'removed-append' ? block('x', 'ours')
        : block('x', 'ours') + '\n' + block('y', 'y2', { begin: '# begin y', end: '# end y' }));
      const suffix = mode === 'recreated-control' ? '' : `tail${eol}`;
      writeFileSync(join(root, 'a.md'), Buffer.concat([read(), Buffer.from(suffix)]));
      next.uninstall({ projectDir: root });
      exact(original + suffix);
    });
  }
  for (const userBytes of ['', `user${eol}`]) {
    it(`R6 U1 created file after creator deleted user=${JSON.stringify(userBytes)} eol=${JSON.stringify(eol)}`, () => {
      setup('');
      rmSync(join(root, 'a.md'));
      const next = run([{ id: 'x', content: 'x1' }, { id: 'y', content: 'y1' }]);
      writeFileSync(join(root, 'a.md'), block('y', 'y1') + userBytes);
      next.uninstall({ projectDir: root });
      if (userBytes) exact(userBytes);
      else assert.equal(existsSync(join(root, 'a.md')), false);
    });
  }
  it(`R6 Sonnet R01 EOF separator activates marker eol=${JSON.stringify(eol)}`, () => {
    setup(`head${eol}`);
    run([{ id: 'y' }]);
    writeFileSync(join(root, 'a.md'), Buffer.concat([read(), Buffer.from(markers('y').begin + '\r')]));
    refuse([{ id: 'y', content: 'updated' }, { id: 'x' }], /Malformed textBlock markers after insertion/);
  });
}

for (const changed of [false, true]) {
  for (const reverse of [false, true]) {
    it(`R6 Sonnet R03 freed markers changed=${changed} reverse=${reverse}`, () => {
      setup('head\n');
      run([{ id: 'x' }, { id: 'z' }]);
      const plan = changed ? [{ id: 'x', markers: markers('z'), content: 'new' }]
        : [{ id: 'x' }, { id: 'w', markers: markers('z'), content: 'new' }];
      const next = run(reverse ? plan.reverse() : plan);
      exact(changed ? append('head\n', block('z', 'new'))
        : append(append('head\n', block('x', 'ours')), block('z', 'new')));
      next.uninstall({ projectDir: root });
      exact('head\n');
    });
  }
}

for (const update of [false, true]) {
  it(`R6 Sonnet R04 legacy edited block update=${update}`, () => {
    setup('head');
    const inserted = block('x', 'ours');
    const edited = append('head', block('x', 'user edit'));
    writeFileSync(join(root, 'a.md'), edited);
    mkdirSync(join(root, '.mi'));
    writeFileSync(join(root, '.mi/manifest.json'), JSON.stringify({ journalVersion: 2,
      effects: [{ type: 'textBlock', id: 'textBlock:a.md#x', beforeState: {
        path: 'a.md', id: 'x', ...markers('x'), fileCreated: false,
        prefix: '\n\n', segmentHash: hashContent(inserted),
      } }], transaction: { state: 'complete' } }));
    const next = update ? run([{ id: 'x', content: 'updated' }]) : make([]);
    exact(edited);
    next.uninstall({ projectDir: root });
    exact(edited);
  });
}

import { describe, it, beforeEach, afterEach } from 'node:test';
import { strict as assert } from 'node:assert';
import { mkdtempSync, readFileSync, writeFileSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createTextBlockEffect } from '../../../src/index.js';
import { hashContent } from '../../../src/hash.js';

const begin = '<!-- BEGIN x -->';
const end = '<!-- END x -->';
const segment = `${begin}\nours\n${end}\n`;

describe('textBlock effect', () => {
  let root, effect, args, file;
  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), 'mi-text-block-'));
    effect = createTextBlockEffect();
    args = { basePath: root, path: 'a.md', id: 'x', content: 'ours\n\n' };
    file = join(root, args.path);
  });
  afterEach(() => rmSync(root, { recursive: true, force: true }));
  const read = (file) => readFileSync(file, 'utf8');

  it('creates a file and removes it with empty parents', () => {
    const state = effect.apply({ ...args, path: 'nested/dir/a.md' });
    assert.deepEqual(state, { path: 'nested/dir/a.md', id: 'x', begin, end,
      fileCreated: true, prefix: '', inserted: Buffer.from(segment).toString('base64'), segmentHash: hashContent(segment) });
    assert.equal(read(join(root, state.path)), segment);
    effect.revert({ basePath: root }, state);
    assert.equal(existsSync(join(root, 'nested')), false);
    assert.equal(existsSync(root), true);
  });

  for (const [name, original, prefix] of [
    ['one newline', 'before\n', '\n'], ['two newlines', 'before\n\n', ''],
    ['no newline', 'before', '\n\n'], ['empty', '', ''],
    ['CRLF', 'before\r\nnext\r\n', '\n'],
    ['non-UTF8 bytes', Buffer.from([0xff, 0x0a]), '\n'],
  ]) {
    it(`round-trips ${name} byte-for-byte`, () => {
      writeFileSync(file, original);
      const bytes = readFileSync(file);
      const state = effect.apply(args);
      assert.equal(state.prefix, prefix);
      assert.deepEqual(Buffer.from(state.inserted, 'base64'), Buffer.from(prefix + segment));
      assert.deepEqual(readFileSync(file), Buffer.concat([bytes, Buffer.from(prefix + segment)]));
      effect.revert({ basePath: root }, state);
      assert.deepEqual(readFileSync(file), bytes);
    });
  }

  for (const [name, original] of [
    ['trailing newline', `${begin}\noriginal\n${end}\n`],
    ['no trailing newline', `${begin}\noriginal\n${end}`],
    ['CRLF', `${begin}\r\noriginal\r\n${end}\r\n`],
    ['surrounding text', `before\n\n${begin}\noriginal\n${end}\nafter`],
  ]) {
    it(`restores an adopted block with ${name} byte-for-byte`, () => {
      const bytes = Buffer.from(original);
      writeFileSync(file, bytes);
      const state = effect.apply(args);
      effect.revert(args, state);
      assert.deepEqual(readFileSync(file), bytes);
      assert.equal(state.prefix, '');
      assert.equal(state.fileCreated, false);
      const start = bytes.indexOf(begin);
      assert.equal(state.replaced, bytes.subarray(start, start + original.slice(start).indexOf(end) + end.length
        + (original.endsWith('after') || original.endsWith('\n') ? (name === 'CRLF' ? 2 : 1) : 0)).toString('base64'));
    });
  }

  for (const migrate of [false, true]) {
    it(`restores adopted bytes after updating, migrate=${migrate}`, () => {
      const original = Buffer.from(`before\n${begin}\r\noriginal\r\n${end}`);
      writeFileSync(file, original);
      const previous = effect.apply(args);
      const state = effect.apply({ ...args, content: 'updated', previous,
        ...(migrate ? { markers: { begin: '# begin', end: '# end' } } : {}) });
      assert.deepEqual(readFileSync(file), Buffer.from(migrate ? original.toString() + '\n\n# begin\nupdated\n# end\n' : `before\n${begin}\nupdated\n${end}\n`));
      effect.revert(args, state);
      assert.deepEqual(readFileSync(file), original);
      assert.equal(state.replaced, migrate ? undefined : previous.replaced);
    });
  }

  it('preserves manual edits to an adopted block', () => {
    writeFileSync(file, `${begin}\noriginal\n${end}\n`);
    const state = effect.apply(args);
    const edited = Buffer.from(segment.replace('ours', 'manual edit'));
    writeFileSync(file, edited);
    effect.revert(args, state);
    assert.deepEqual(readFileSync(file), edited);
    assert.equal(typeof state.replaced, 'string');
  });

  it('preserves content before and third-party content appended after the block', () => {
    writeFileSync(file, 'before');
    const state = effect.apply(args);
    assert.equal(read(file), 'before\n\n' + segment);
    writeFileSync(file, read(file) + 'after\r\n');
    effect.revert({ basePath: root }, state);
    assert.equal(read(file), 'beforeafter\r\n');
  });

  it('updates in place carrying ownership and prefix forward', () => {
    writeFileSync(file, 'before\n');
    const first = effect.apply(args);
    writeFileSync(file, read(file) + 'after');
    const second = effect.apply({ ...args, content: 'new', previous: first });
    assert.equal(read(file), `before\n\n${begin}\nnew\n${end}\nafter`);
    effect.revert({ basePath: root }, second);
    assert.equal(read(file), 'before\nafter');
  });

  it('preserves edited blocks, including changes to line endings', () => {
    const state = effect.apply(args);
    for (const edited of [segment.replace('ours', 'mine'), segment.replaceAll('\n', '\r\n')]) {
      writeFileSync(file, edited);
      effect.revert({ basePath: root }, state);
      assert.equal(read(file), edited);
    }
  });

  it('does nothing when file or block is gone', () => {
    const state = effect.apply(args);
    rmSync(file);
    effect.revert({ basePath: root }, state);
    assert.equal(existsSync(file), false);
    writeFileSync(file, 'replacement');
    effect.revert({ basePath: root }, state);
    assert.equal(read(file), 'replacement');
  });

  it('reverts ids independently in the same file', () => {
    writeFileSync(file, 'before\n');
    const x = effect.apply(args);
    const y = effect.apply({ ...args, id: 'y', content: 'other' });
    effect.revert({ basePath: root }, x);
    assert.equal(read(file), 'before\n\n<!-- BEGIN y -->\nother\n<!-- END y -->\n');
    effect.revert({ basePath: root }, y);
    assert.equal(read(file), 'before\n');
  });

  for (const malformed of [`${begin}\n`, `${begin}\n${begin}\n${end}\n`,
    `${end}\n${begin}\n`, `${begin}\n${end}\n${end}\n`]) {
    it(`refuses malformed markers ${JSON.stringify(malformed)}`, () => {
      const state = effect.apply(args);
      writeFileSync(file, malformed);
      assert.throws(() => effect.apply(args), /a\.md/);
      assert.equal(read(file), malformed);
      effect.revert({ basePath: root }, state);
      assert.equal(read(file), malformed);
    });
  }

  it('validates ids, content and marker lines before writing', () => {
    for (const id of ['', 'a b', 'x\n', null, 1]) assert.throws(() => effect.apply({ ...args, id }));
    for (const content of [null, 1, begin, `a\n${end}\r\nb`]) {
      assert.throws(() => effect.apply({ ...args, content }));
    }
    for (const markers of [{ begin: 'a\nb', end: 'end' }, { begin: 'b', end: 'e\r' }, {}]) {
      assert.throws(() => effect.apply({ ...args, markers }));
    }
    assert.equal(existsSync(file), false);
  });

  it('matches only whole marker lines and accepts custom markers', () => {
    const markers = { begin: '# >>> x >>>', end: '# <<< x <<<' };
    const original = `inline ${markers.begin}\n`;
    writeFileSync(file, original);
    const state = effect.apply({ ...args, markers });
    assert.equal(read(file), `${original}\n${markers.begin}\nours\n${markers.end}\n`);
    effect.revert({ basePath: root }, state);
    assert.equal(read(file), original);
  });

  it('replaces an existing CRLF block without touching surrounding bytes', () => {
    writeFileSync(file, `before\r\n${begin}\r\nold\r\n${end}\r\nafter`);
    const state = effect.apply(args);
    assert.equal(state.prefix, '');
    assert.equal(state.fileCreated, false);
    assert.equal(read(file), 'before\r\n' + segment + 'after');
  });

  it('preserves a changed prefix and supports ctx.path fallback', () => {
    writeFileSync(file, 'before');
    const state = effect.apply(args);
    writeFileSync(file, 'changed\n' + segment);
    delete state.path;
    effect.revert(args, state);
    assert.equal(read(file), 'changed\n');
  });

  it('rejects escaping paths on apply and revert', () => {
    assert.throws(() => effect.apply({ ...args, path: '../x' }), /outside basePath/);
    assert.throws(() => effect.revert(args, { path: '../x' }), /outside basePath/);
  });

  for (const mismatch of [{ path: 'other.md' }]) {
    it(`ignores previous with mismatched ${Object.keys(mismatch)[0]}`, () => {
      const previous = { ...effect.apply(args), ...mismatch, prefix: '\n\n' };
      writeFileSync(file, '\n\n' + segment);
      const state = effect.apply({ ...args, previous });
      assert.equal(state.fileCreated, false);
      assert.equal(state.prefix, '');
      effect.revert(args, state);
      assert.equal(read(file), '\n\n' + segment);
    });
  }

  it('refuses a different id adopting the restored original markers', () => {
    writeFileSync(file, segment.replace('ours', 'original'));
    const previous = effect.apply(args);
    const bytes = readFileSync(file);
    assert.throws(() => effect.apply({ ...args, id: 'y', markers: { begin, end }, previous }), /a\.md/);
    assert.deepEqual(readFileSync(file), bytes);
  });

  for (const markers of [{ begin: '', end }, { begin, end: '' },
    { begin: ' \t ', end }, { begin, end: ' \t ' }]) {
    it(`rejects empty markers ${JSON.stringify(markers)}`, () => {
      writeFileSync(file, 'unchanged');
      assert.throws(() => effect.apply({ ...args, markers }), /markers/);
      assert.equal(read(file), 'unchanged');
    });
  }

  it('replaces an end marker at EOF and restores the original block', () => {
    writeFileSync(file, 'before\n' + segment.trimEnd());
    const state = effect.apply(args);
    assert.equal(read(file), 'before\n' + segment);
    effect.revert(args, state);
    assert.equal(read(file), 'before\n' + segment.trimEnd());
  });

  it('rejects an unmatched begin marker at EOF without writing', () => {
    const original = 'before\n' + begin;
    writeFileSync(file, original);
    assert.throws(() => effect.apply(args), /a\.md/);
    assert.equal(read(file), original);
  });

  it('carries file creation ownership through a second apply', () => {
    const previous = effect.apply(args);
    const state = effect.apply({ ...args, content: 'updated', previous });
    assert.equal(state.fileCreated, true);
    effect.revert(args, state);
    assert.equal(existsSync(file), false);
  });

  it('rejects identical markers', () => {
    assert.throws(() => effect.apply({ ...args, markers: { begin, end: begin } }), /markers/);
    assert.equal(existsSync(file), false);
  });

  it('rejects content differing from either marker only by a trailing CR', () => {
    for (const marker of [begin, end]) {
      assert.throws(() => effect.apply({ ...args, content: marker + '\r' }), /marker line/);
    }
    assert.equal(existsSync(file), false);
  });

  const markers = { begin: '# begin x', end: '# end x' };
  const migrated = '# begin x\nnew\n# end x\n';
  for (const old of ['unedited', 'edited', 'gone']) {
    it(`migrates markers when the old block is ${old}`, () => {
      writeFileSync(file, 'before');
      const previous = effect.apply(args);
      if (old === 'edited') writeFileSync(file, read(file).replace('ours', 'mine'));
      if (old === 'gone') writeFileSync(file, 'before');
      const retained = old === 'edited' ? 'before\n\n' + segment.replace('ours', 'mine') : 'before';
      const state = effect.apply({ ...args, markers, content: 'new', previous });
      assert.equal(read(file), retained + (old === 'edited' ? '\n' : '\n\n') + migrated);
      effect.revert(args, state);
      assert.equal(read(file), retained);
    });
  }

  for (const order of ['old-first', 'new-first']) {
    for (const prefixMatches of [true, false]) {
      it(`refuses coexisting target blocks ${order}, prefixMatches=${prefixMatches}`, () => {
        writeFileSync(file, 'before');
        const previous = effect.apply(args);
        const spacing = prefixMatches ? '\n\n' : '\n';
        const old = spacing + segment;
        const newer = '# begin x\nstale\n# end x\n';
        writeFileSync(file, 'before' + (order === 'old-first' ? old + 'middle\n' + newer : '\n' + newer + 'middle' + old) + 'after');
        const bytes = readFileSync(file);
        assert.throws(() => effect.apply({ ...args, markers, content: 'new', previous }), /a\.md/);
        assert.deepEqual(readFileSync(file), bytes);
      });
    }
  }


  for (const changed of [{ begin: '# begin x', end }, { begin, end: '# end x' }]) {
    it(`migrates a single changed marker ${JSON.stringify(changed)}`, () => {
      writeFileSync(file, 'before');
      const previous = effect.apply(args);
      writeFileSync(file, read(file) + 'after');
      const state = effect.apply({ ...args, markers: changed, previous });
      assert.deepEqual(readFileSync(file), Buffer.from(`beforeafter\n\n${changed.begin}\nours\n${changed.end}\n`));
      effect.revert(args, state);
      assert.equal(read(file), 'beforeafter');
    });
  }

  it('refuses existing new markers when the old block was edited', () => {
    const previous = effect.apply(args);
    const edited = segment.replace('ours', 'mine');
    writeFileSync(file, edited + '\n# begin x\nstale\n# end x\n');
    const bytes = readFileSync(file);
    assert.throws(() => effect.apply({ ...args, markers, content: 'new', previous }), /a\.md/);
    assert.deepEqual(readFileSync(file), bytes);
  });

  it('carries created-file ownership when old markers are gone during migration', () => {
    const previous = effect.apply(args);
    writeFileSync(file, '');
    const state = effect.apply({ ...args, markers, content: 'new', previous });
    assert.equal(state.fileCreated, true);
    assert.equal(read(file), migrated);
    effect.revert(args, state);
    assert.equal(existsSync(file), false);
  });

});

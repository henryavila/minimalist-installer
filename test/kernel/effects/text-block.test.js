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
      fileCreated: true, prefix: '', segmentHash: hashContent(segment) });
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
      assert.deepEqual(readFileSync(file), Buffer.concat([bytes, Buffer.from(prefix + segment)]));
      effect.revert({ basePath: root }, state);
      assert.deepEqual(readFileSync(file), bytes);
    });
  }

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
});

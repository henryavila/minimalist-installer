import { hashContent } from '../../hash.js';
import {
  assertLexicalWithinBase,
  existsNoFollow,
  readFileNoFollow,
  writeFileNoFollow,
  unlinkNoFollow,
  pruneEmptyParentsNoFollow,
} from '../../path-safety.js';

// Keep byte offsets so even non-UTF8 bytes outside the block survive unchanged.
// A segment includes the end marker's newline, when present.
const findBlock = (bytes, begin, end) => {
  const begins = [];
  const ends = [];
  const beginBytes = Buffer.from(begin);
  const endBytes = Buffer.from(end);
  for (let start = 0; start < bytes.length;) {
    const newline = bytes.indexOf(10, start);
    const stop = newline === -1 ? bytes.length : newline;
    const next = newline === -1 ? bytes.length : newline + 1;
    const line = bytes.subarray(start, bytes[stop - 1] === 13 ? stop - 1 : stop);
    if (line.equals(beginBytes)) begins.push({ start, next });
    if (line.equals(endBytes)) ends.push({ start, next });
    start = next;
  }
  if (begins.length === 0 && ends.length === 0) return null;
  if (begins.length !== 1 || ends.length !== 1 || begins[0].start >= ends[0].start) {
    return { malformed: true };
  }
  return { start: begins[0].start, end: ends[0].next };
};

// Both migration and uninstall settle exactly the same owned substitution.
const restoreBlock = (bytes, block, state) => {
  if (state.replaced !== undefined) {
    return Buffer.concat([
      bytes.subarray(0, block.start), Buffer.from(state.replaced, 'base64'), bytes.subarray(block.end),
    ]);
  }
  const prefix = Buffer.from(state.prefix ?? '');
  const start = block.start >= prefix.length
    && bytes.subarray(block.start - prefix.length, block.start).equals(prefix)
    ? block.start - prefix.length : block.start;
  return Buffer.concat([bytes.subarray(0, start), bytes.subarray(block.end)]);
};

export const createTextBlockEffect = () => ({
  type: 'textBlock',

  apply({ basePath, path, id, content, markers, previous }) {
    assertLexicalWithinBase(basePath, path);
    if (typeof id !== 'string' || !/^[A-Za-z0-9._:-]+$/.test(id)) {
      throw new Error('textBlock id must be a non-empty identifier');
    }
    if (typeof content !== 'string') throw new Error('textBlock content must be a string');
    const { begin, end } = markers ?? {
      begin: `<!-- BEGIN ${id} -->`, end: `<!-- END ${id} -->`,
    };
    if ([begin, end].some((line) => typeof line !== 'string' || !line.trim() || /[\r\n]/.test(line))
      || begin === end) {
      throw new Error('textBlock markers must be distinct single-line strings');
    }
    if (content.split('\n').some((line) => {
      const normalized = line.replace(/\r$/, '');
      return normalized === begin || normalized === end;
    })) {
      throw new Error('textBlock content must not contain a marker line');
    }
    // The Driver may fall back to another effect's positional before-state.
    if (previous?.path !== path || previous?.id !== id) previous = undefined;

    const segment = `${begin}\n${content.replace(/[\r\n]+$/, '')}\n${end}\n`;
    const created = !existsNoFollow(basePath, path);
    let bytes = created ? Buffer.alloc(0) : readFileNoFollow(basePath, path, null);
    let block;
    let ownership;
    if (previous) {
      const old = findBlock(bytes, previous.begin, previous.end);
      if (old?.malformed) throw new Error(`Malformed textBlock markers at "${path}"`);
      const sameMarkers = previous.begin === begin && previous.end === end;
      const owned = old && hashContent(bytes.subarray(old.start, old.end)) === previous.segmentHash;
      // An edited block is no longer ours to overwrite, even on update.
      if (old && !owned && sameMarkers) return { ...previous, id };
      if (owned) {
        if (sameMarkers) {
          block = old;
          ownership = previous;
        } else {
          // Resolve the old adoption at its own location before adopting a
          // different block. A snapshot must never move to another identity.
          const withoutOld = Buffer.concat([bytes.subarray(0, old.start), bytes.subarray(old.end)]);
          const newer = findBlock(withoutOld, begin, end);
          if (newer?.malformed) throw new Error(`Malformed textBlock markers at "${path}"`);
          if (newer) {
            bytes = restoreBlock(bytes, old, previous);
          } else {
            block = old;
            ownership = previous;
          }
        }
      }
      // Missing or edited old blocks relinquish their snapshot. A fresh append
      // owns only its new segment and spacing, never the old adopted bytes.
    }
    block ??= findBlock(bytes, begin, end);
    if (block?.malformed) throw new Error(`Malformed textBlock markers at "${path}"`);

    let prefix;
    let result;
    const replaced = ownership ? ownership.replaced
      : block ? bytes.subarray(block.start, block.end).toString('base64') : undefined;
    if (block) {
      prefix = ownership?.prefix ?? '';
      result = Buffer.concat([
        bytes.subarray(0, block.start), Buffer.from(segment), bytes.subarray(block.end),
      ]);
    } else {
      prefix = bytes.length === 0 || bytes.subarray(-2).equals(Buffer.from('\n\n'))
        ? '' : bytes.at(-1) === 10 ? '\n' : '\n\n';
      result = Buffer.concat([bytes, Buffer.from(prefix + segment)]);
    }
    writeFileNoFollow(basePath, path, result, { atomic: true });
    // The latest journal must retain ownership established by earlier installs.
    return {
      path, id, begin, end,
      fileCreated: created || Boolean(previous?.fileCreated),
      prefix,
      segmentHash: hashContent(segment),
      ...(replaced !== undefined ? { replaced } : {}),
    };
  },

  revert(ctx, beforeState) {
    const { basePath } = ctx;
    const path = beforeState.path ?? ctx.path;
    assertLexicalWithinBase(basePath, path);
    if (!existsNoFollow(basePath, path)) return;
    const bytes = readFileNoFollow(basePath, path, null);
    const block = findBlock(bytes, beforeState.begin, beforeState.end);
    if (!block || block.malformed) return;
    if (hashContent(bytes.subarray(block.start, block.end)) !== beforeState.segmentHash) return;

    const result = restoreBlock(bytes, block, beforeState);
    if (beforeState.fileCreated && result.length === 0) {
      unlinkNoFollow(basePath, path);
      pruneEmptyParentsNoFollow(basePath, path);
    } else {
      writeFileNoFollow(basePath, path, result, { atomic: true });
    }
  },
});

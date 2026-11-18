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
const findBlock = (bytes, begin, end, state) => {
  const begins = [];
  const ends = [];
  const beginBytes = Buffer.from(begin);
  const endBytes = Buffer.from(end);
  for (let start = 0; start < bytes.length;) {
    const newline = bytes.indexOf(10, start);
    const stop = newline === -1 ? bytes.length : newline;
    const next = newline === -1 ? bytes.length : newline + 1;
    const line = bytes.subarray(start, newline !== -1 && bytes[stop - 1] === 13 ? stop - 1 : stop);
    if (line.equals(beginBytes)) begins.push({ start, next });
    if (line.equals(endBytes)) ends.push({ start, next, coreEnd: stop });
    start = next;
  }
  if (begins.length === 0 && ends.length === 0) return null;
  if (begins.length !== 1 || ends.length !== 1 || begins[0].start >= ends[0].start) {
    return { malformed: true };
  }
  return { start: begins[0].start, end: state?.borrowedTerminator ? ends[0].coreEnd : ends[0].next };
};

// New journals retain the inserted bytes; 0e12991 journals retain their hash.
const ownsBlock = (bytes, block, state) => state.inserted === undefined
  ? hashContent(bytes.subarray(block.start, block.end)) === state.segmentHash
  : bytes.subarray(block.start, block.end).equals(
    Buffer.from(state.inserted, 'base64').subarray(Buffer.byteLength(state.prefix ?? '')));

// Undo only the recorded substitution, including its exact separator bytes.
const restoreBlock = (bytes, block, state) => {
  const prefix = Buffer.from(state.prefix ?? '');
  const start = block.start >= prefix.length
    && bytes.subarray(block.start - prefix.length, block.start).equals(prefix)
    ? block.start - prefix.length : block.start;
  return Buffer.concat([bytes.subarray(0, start),
    state.replaced === undefined ? Buffer.alloc(0) : Buffer.from(state.replaced, 'base64'),
    bytes.subarray(block.end)]);
};

const textBlockPeers = Symbol('textBlockPeers');
const freshInstall = Symbol('freshInstall');
const createdFile = Symbol('createdFile');

const prepare = (args, input, peers = []) => {
  let { basePath, path, id, content, markers, previous } = args;
  assertLexicalWithinBase(basePath, path);
  if (typeof id !== 'string' || !/^[A-Za-z0-9._:-]+$/.test(id)) {
    throw new Error(`textBlock id must be a non-empty identifier at "${path}"`);
  }
  if (typeof content !== 'string') throw new Error(`textBlock content must be a string at "${path}"`);
  const { begin, end } = markers ?? {
    begin: `<!-- BEGIN ${id} -->`, end: `<!-- END ${id} -->`,
  };
  if ([begin, end].some((line) => typeof line !== 'string' || !line.trim() || /[\r\n]/.test(line))
    || begin === end) {
    throw new Error(`textBlock markers must be distinct single-line strings at "${path}"`);
  }
  if (content.split('\n').some((line) => {
    const normalized = line.replace(/\r$/, '');
    return normalized === begin || normalized === end;
  })) {
    throw new Error(`textBlock content must not contain a marker line at "${path}"`);
  }
  if (previous?.path !== path) previous = undefined;

  const segment = `${begin}\n${content.replace(/[\r\n]+$/, '')}\n${end}\n`;
  const created = input === null;
  let bytes = input ?? Buffer.alloc(0);
  let block;
  let ownership;
  let fresh = args[freshInstall];
  if (previous) {
    const old = findBlock(bytes, previous.begin, previous.end, previous);
    if (old?.malformed) throw new Error(`Malformed textBlock markers at "${path}"`);
    const sameIdentity = previous.id === id && previous.begin === begin && previous.end === end;
    const owned = old && ownsBlock(bytes, old, previous);
    if (sameIdentity) {
      // Hand edits relinquish the block; update and revert both leave it alone.
      if (old && !owned) return { state: previous, result: bytes, unchanged: true };
      if (owned) { block = old; ownership = previous; }
    } else {
      if (owned) bytes = restoreBlock(bytes, old, previous);
      fresh = true;
    }
  }
  block ??= findBlock(bytes, begin, end);
  if (block?.malformed) throw new Error(`Malformed textBlock markers at "${path}"`);
  if (fresh && block) throw new Error(`textBlock fresh install markers already present at "${path}"`);

  // A peer's prefix may begin at this end marker. Those bytes are never
  // adopted or hashed by this block, even when they terminate its line.
  let borrowedTerminator = ownership?.borrowedTerminator;
  if (block && !ownership) {
    for (const peer of peers) {
      if (peer.path !== path || !peer.prefix) continue;
      const adjacent = findBlock(bytes, peer.begin, peer.end, peer);
      const prefix = Buffer.from(peer.prefix);
      if (adjacent && !adjacent.malformed
        && bytes.subarray(adjacent.start - prefix.length, adjacent.start).equals(prefix)
        && adjacent.start - prefix.length === block.end - 1) {
        block = { ...block, end: block.end - 1 };
        borrowedTerminator = true;
      }
    }
  }
  const writtenSegment = borrowedTerminator ? segment.slice(0, -1) : segment;
  let prefix;
  let result;
  const replaced = ownership ? ownership.replaced
    : block ? bytes.subarray(block.start, block.end).toString('base64') : undefined;
  if (block) {
    prefix = ownership?.prefix ?? '';
    result = Buffer.concat([
      bytes.subarray(0, block.start), Buffer.from(writtenSegment), bytes.subarray(block.end),
    ]);
  } else {
    prefix = bytes.length === 0 || bytes.subarray(-2).equals(Buffer.from('\n\n'))
      ? '' : bytes.at(-1) === 10 ? '\n' : '\n\n';
    result = Buffer.concat([bytes, Buffer.from(prefix + segment)]);
  }
  if (findBlock(result, begin, end)?.malformed) {
    throw new Error(`Malformed textBlock markers after insertion at "${path}"`);
  }
  // The latest journal must retain ownership established by earlier installs.
  return { result, state: {
    path, id, begin, end,
    fileCreated: created || Boolean(previous?.fileCreated) || Boolean(args[createdFile]),
    prefix,
    inserted: Buffer.from(prefix + writtenSegment).toString('base64'),
    segmentHash: hashContent(writtenSegment),
    ...(borrowedTerminator ? { borrowedTerminator: true } : {}),
    ...(replaced !== undefined ? { replaced } : {}),
  } };
};

// Plan all removals before fresh installs. Keep retained effects in journal order:
// reverse replay must undo later separators before earlier adoptions.
export const prepareTextBlockPlan = (entries, priorStates, basePath) => {
  const files = new Map();
  const read = (args) => {
    const key = JSON.stringify([args.basePath, args.path]);
    assertLexicalWithinBase(args.basePath, args.path);
    if (!files.has(key)) files.set(key, existsNoFollow(args.basePath, args.path)
      ? readFileNoFollow(args.basePath, args.path, null) : null);
    return [key, files.get(key)];
  };
  // Validate argument shapes first, then all content against all marker pairs.
  const definitions = entries.map((args) => prepare(args, null).state);
  for (const args of entries) {
    const all = [...definitions, ...priorStates].filter((s) => s.path === args.path);
    const lines = args.content.split('\n').map((line) => line.replace(/\r$/, ''));
    if (all.some((s) => lines.includes(s.begin) || lines.includes(s.end))) {
      throw new Error(`textBlock content must not contain a marker line at "${args.path}"`);
    }
    const [, bytes] = read(args);
    const ranges = [];
    // Pairs sharing an old marker are checked after removing their predecessor.
    for (const state of all.filter((s) => priorStates.includes(s)
      || !priorStates.some((old) => old.path === s.path
        && [old.begin, old.end].some((m) => m === s.begin || m === s.end)))) {
      const block = findBlock(bytes ?? Buffer.alloc(0), state.begin, state.end, state);
      if (block?.malformed) throw new Error(`Malformed textBlock markers at "${args.path}"`);
      if (block && ranges.some((r) => r.start !== block.start && r.start < block.end && block.start < r.end)) {
        throw new Error(`Nested textBlock markers at "${args.path}"`);
      }
      if (block) ranges.push(block);
    }
  }
  const retained = new Set(entries.filter((args, i) => args.previous
    && args.previous.id === args.id && args.previous.begin === definitions[i].begin
    && args.previous.end === definitions[i].end).map((args) => args.previous));
  const removals = priorStates.filter((state) => !retained.has(state)).reverse();
  for (const state of removals) {
    const args = entries.find((a) => a.path === state.path) ?? { basePath, path: state.path };
    const [key, bytes] = read(args);
    if (bytes === null) continue;
    const block = findBlock(bytes, state.begin, state.end, state);
    if (block?.malformed) throw new Error(`Malformed textBlock markers at "${state.path}"`);
    if (block && ownsBlock(bytes, block, state)) {
      const result = restoreBlock(bytes, block, state);
      files.set(key, state.fileCreated && result.length === 0 ? null : result);
    }
  }
  const peers = priorStates.filter((s) => retained.has(s));
  const ordered = [...entries].sort((a, b) => {
    const rank = (args) => retained.has(args.previous) ? priorStates.indexOf(args.previous) : priorStates.length;
    return rank(a) - rank(b);
  });
  const prepared = ordered.map((args) => {
    const [key, bytes] = read(args);
    const changed = args.previous && !retained.has(args.previous);
    const fresh = !retained.has(args.previous) && (changed || removals.some((s) => s.path === args.path));
    const next = { ...args,
      previous: retained.has(args.previous) ? args.previous : undefined,
      [freshInstall]: fresh,
      [createdFile]: priorStates.some((s) => s.path === args.path && s.fileCreated),
      [textBlockPeers]: [...peers],
    };
    const result = prepare(next, bytes, peers);
    files.set(key, result.result);
    const old = peers.indexOf(args.previous);
    if (old !== -1) peers.splice(old, 1);
    peers.push(result.state);
    return next;
  });
  // Inserting a separator must not turn an EOF user line into a marker.
  for (const args of prepared) {
    const [, bytes] = read(args);
    const { begin, end } = definitions[entries.findIndex((a) => a.path === args.path && a.id === args.id)];
    if (findBlock(bytes, begin, end)?.malformed) {
      throw new Error(`Malformed textBlock markers after insertion at "${args.path}"`);
    }
  }
  return { entries: prepared, removals };
};

/**
 * A deliberately narrow, byte-exact textBlock contract:
 * 1. Markers match whole lines only, excluding LF or CRLF, never prefixes.
 * 2. An absent block is appended; inserted bytes include every separator.
 * 3. Only the first install may adopt one existing block, saving its exact bytes.
 * 4. Same id and markers update in place, keeping the first before-state.
 * 5. Changed id/markers undo the old substitution, then install fresh; existing
 *    target marker lines refuse the plan. No adoption or ownership transfer.
 * 6. Refuse duplicate/unbalanced/nested markers, empty/equal markers, planned
 *    id/marker collisions, or content with any planned/journaled marker line
 *    on the file. Errors name the file and reason, independently of plan order.
 * 7. The Driver validates the whole plan before changing files or the manifest.
 * 8. Hand-edited, missing or broken blocks are left alone on revert (no-op),
 *    as in the original card; updates also preserve hand-edited block content.
 * 9. Legacy 0e12991 journal entries still revert using their recorded hashes.
 */
export const createTextBlockEffect = () => ({
  type: 'textBlock',
  apply(args) {
    const { basePath, path } = args;
    assertLexicalWithinBase(basePath, path);
    const prepared = prepare(args,
      existsNoFollow(basePath, path) ? readFileNoFollow(basePath, path, null) : null, args[textBlockPeers]);
    if (!prepared.unchanged) writeFileNoFollow(basePath, path, prepared.result, { atomic: true });
    return prepared.state;
  },

  revert(ctx, beforeState) {
    const { basePath } = ctx;
    const path = beforeState.path ?? ctx.path;
    assertLexicalWithinBase(basePath, path);
    if (!existsNoFollow(basePath, path)) return;
    const bytes = readFileNoFollow(basePath, path, null);
    const block = findBlock(bytes, beforeState.begin, beforeState.end, beforeState);
    if (!block || block.malformed) return;
    if (!ownsBlock(bytes, block, beforeState)) return;

    const result = restoreBlock(bytes, block, beforeState);
    if (beforeState.fileCreated && result.length === 0) {
      unlinkNoFollow(basePath, path);
      pruneEmptyParentsNoFollow(basePath, path);
    } else {
      writeFileNoFollow(basePath, path, result, { atomic: true });
    }
  },
});

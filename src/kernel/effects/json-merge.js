import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmdirSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { dirname, join, resolve, sep } from 'node:path';
import { isDeepStrictEqual } from 'node:util';

const resolveWithinBase = (basePath, path) => {
  const base = resolve(basePath);
  const absPath = join(basePath, path);
  const resolved = resolve(absPath);
  if (resolved !== base && !resolved.startsWith(base + sep)) {
    throw new Error(`Refusing to operate outside basePath: "${path}"`);
  }
  return absPath;
};

const pruneEmptyParents = (absPath, basePath) => {
  const base = resolve(basePath);
  let parent = dirname(resolve(absPath));

  while (parent !== base && parent !== '.') {
    try {
      if (readdirSync(parent).length === 0) {
        rmdirSync(parent);
        parent = dirname(parent);
      } else {
        break;
      }
    } catch {
      break;
    }
  }
};

const hasOwn = (object, key) => Object.prototype.hasOwnProperty.call(object, key);

const isPlainObject = (value) => {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return false;
  }

  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
};

const isScalar = (value) => (
  value === null
  || typeof value === 'string'
  || typeof value === 'number'
  || typeof value === 'boolean'
);

const isEmptyRoot = (value) => isPlainObject(value) && Object.keys(value).length === 0;

const isEmptyContainer = (value) => {
  if (Array.isArray(value)) {
    return value.length === 0;
  }

  return isPlainObject(value) && Object.keys(value).length === 0;
};

const formatPath = (path) => (path.length === 0 ? '<root>' : path.join('.'));

const cloneJsonValue = (value) => JSON.parse(JSON.stringify(value));

const parseJsonFile = (absPath, path) => {
  try {
    return JSON.parse(readFileSync(absPath, 'utf8'));
  } catch (error) {
    if (error instanceof SyntaxError) {
      throw new Error(`Unable to parse JSON at "${path}": ${error.message}`);
    }
    throw error;
  }
};

const navigate = (root, path) => {
  let current = root;

  for (const segment of path) {
    if (current === null || typeof current !== 'object' || !hasOwn(current, segment)) {
      return undefined;
    }
    current = current[segment];
  }

  return current;
};

const assertSupportedDeltaValue = (value, path) => {
  if (isScalar(value) || Array.isArray(value) || isPlainObject(value)) {
    return;
  }

  throw new Error(`Unsupported JSON delta value at ${formatPath(path)}`);
};

const appendArrayItems = ({ targetArray, deltaArray, path, inserts }) => {
  for (const item of deltaArray) {
    assertSupportedDeltaValue(item, path);

    if (targetArray.some((existing) => isDeepStrictEqual(existing, item))) {
      continue;
    }

    const value = cloneJsonValue(item);
    targetArray.push(value);
    inserts.push({ kind: 'arrayItem', path: [...path], value: cloneJsonValue(item) });
  }
};

const mergeObject = ({ target, delta, path, inserts, createdContainers }) => {
  for (const [key, deltaValue] of Object.entries(delta)) {
    const keyPath = [...path, key];
    assertSupportedDeltaValue(deltaValue, keyPath);

    if (!hasOwn(target, key)) {
      if (isPlainObject(deltaValue)) {
        target[key] = {};
        createdContainers.push(keyPath);
        mergeObject({
          target: target[key],
          delta: deltaValue,
          path: keyPath,
          inserts,
          createdContainers,
        });
      } else if (Array.isArray(deltaValue)) {
        target[key] = [];
        createdContainers.push(keyPath);
        appendArrayItems({
          targetArray: target[key],
          deltaArray: deltaValue,
          path: keyPath,
          inserts,
        });
      } else {
        target[key] = cloneJsonValue(deltaValue);
        inserts.push({ kind: 'key', path: [...path], key });
      }
      continue;
    }

    const targetValue = target[key];

    if (isPlainObject(deltaValue)) {
      if (!isPlainObject(targetValue)) {
        throw new Error(`Cannot merge object into existing non-object at ${formatPath(keyPath)}`);
      }

      mergeObject({
        target: targetValue,
        delta: deltaValue,
        path: keyPath,
        inserts,
        createdContainers,
      });
    } else if (Array.isArray(deltaValue)) {
      if (!Array.isArray(targetValue)) {
        throw new Error(`Cannot merge array into existing non-array at ${formatPath(keyPath)}`);
      }

      appendArrayItems({
        targetArray: targetValue,
        deltaArray: deltaValue,
        path: keyPath,
        inserts,
      });
    } else if (!isDeepStrictEqual(targetValue, deltaValue)) {
      throw new Error(`Cannot overwrite existing scalar at ${formatPath(keyPath)}`);
    }
  }
};

const deletePath = (root, path) => {
  const parentPath = path.slice(0, -1);
  const key = path.at(-1);
  const parent = navigate(root, parentPath);

  if (parent !== undefined && parent !== null && typeof parent === 'object') {
    delete parent[key];
  }
};

const pruneCreatedContainers = (root, createdContainers) => {
  const longestFirst = [...createdContainers].sort((a, b) => b.length - a.length);

  for (const path of longestFirst) {
    const container = navigate(root, path);
    if (container !== undefined && isEmptyContainer(container)) {
      deletePath(root, path);
    }
  }
};

export const createJsonMergeEffect = () => ({
  type: 'jsonMerge',

  apply({ basePath, path, delta }) {
    const absPath = resolveWithinBase(basePath, path);
    const fileCreated = !existsSync(absPath);
    const target = fileCreated ? {} : parseJsonFile(absPath, path);

    if (!isPlainObject(target)) {
      throw new Error(`JSON merge target must be an object at "${path}"`);
    }
    if (!isPlainObject(delta)) {
      throw new Error('JSON merge delta must be an object');
    }

    const inserts = [];
    const createdContainers = [];
    mergeObject({ target, delta, path: [], inserts, createdContainers });

    mkdirSync(dirname(absPath), { recursive: true });
    writeFileSync(absPath, JSON.stringify(target, null, 2) + '\n', 'utf8');

    return { fileCreated, inserts, createdContainers };
  },

  revert({ basePath, path }, beforeState) {
    const absPath = resolveWithinBase(basePath, path);
    if (!existsSync(absPath)) return;

    const target = parseJsonFile(absPath, path);

    for (const insert of [...beforeState.inserts].reverse()) {
      if (insert.kind === 'key') {
        const parent = navigate(target, insert.path);
        if (isPlainObject(parent) && hasOwn(parent, insert.key)) {
          delete parent[insert.key];
        }
      } else if (insert.kind === 'arrayItem') {
        const array = navigate(target, insert.path);
        if (!Array.isArray(array)) continue;

        const index = array.findIndex((item) => isDeepStrictEqual(item, insert.value));
        if (index !== -1) {
          array.splice(index, 1);
        }
      }
    }

    pruneCreatedContainers(target, beforeState.createdContainers);

    if (beforeState.fileCreated && isEmptyRoot(target)) {
      unlinkSync(absPath);
      pruneEmptyParents(absPath, basePath);
      return;
    }

    writeFileSync(absPath, JSON.stringify(target, null, 2) + '\n', 'utf8');
  },
});

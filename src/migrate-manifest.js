/**
 * Journal v1 → v2 migration.
 *
 * Ambiguous v1 artifacts (no stable effect id) are preserved as-is in the
 * effects array without inventing ordinal→id mappings that could clobber
 * ownership. Callers may tag entries as unmanaged when identity is unknown.
 */
import { readEffects, JOURNAL_VERSION } from './kernel/journal.js';
import { stableEffectId } from './kernel/journal.js';

/**
 * @param {object|null} manifest
 * @returns {{ manifest: object|null, ambiguous: number }}
 */
export function migrateManifestToV2(manifest) {
  if (manifest == null) return { manifest: null, ambiguous: 0 };
  if (manifest.journalVersion >= 2) {
    return { manifest, ambiguous: 0 };
  }

  let ambiguous = 0;
  const effects = readEffects(manifest).map((entry) => {
    if (entry.id != null) return entry;
    // Without args we cannot reconstruct a path-stable id for jsonMerge etc.
    // Mark unmanaged rather than inventing a type-only id that would collide.
    ambiguous += 1;
    return {
      ...entry,
      id: entry.id ?? `unmanaged:${entry.type}:${ambiguous}`,
      unmanaged: true,
    };
  });

  return {
    manifest: {
      ...manifest,
      journalVersion: JOURNAL_VERSION,
      effects,
    },
    ambiguous,
  };
}

// Re-export for convenience.
export { stableEffectId, JOURNAL_VERSION };

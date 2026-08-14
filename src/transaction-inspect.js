/**
 * Read-only transaction inspection (no mutation of disk).
 */
export {
  inspectTransaction,
  describeRecovery,
  readJournaledEffects,
  assertNoIncompleteTransaction,
} from './recovery.js';

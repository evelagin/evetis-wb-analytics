/** Этап C: читатель владения для тестов cli — поток уже принадлежит Cloud Run (gate пропускает). */
import type { OwnershipReader, OwnershipSnapshot } from '../src/ownership/gate.js';

export function fixedOwner(snap: OwnershipSnapshot | null | Error): OwnershipReader {
  return {
    read: async () => {
      if (snap instanceof Error) throw snap;
      return snap;
    },
  };
}

export const CLOUD_RUN_OWNER = (): OwnershipReader => fixedOwner({ owner: 'CLOUD_RUN', epoch: 1, state: 'COMPLETED' });

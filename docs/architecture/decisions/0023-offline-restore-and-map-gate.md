# ADR-0023: Restore a stopped store offline, and gate the node on its map

<!-- --8<-- [start:body] -->
**Status:** Proposed · **Date:** 2026-11-05

**Context**

[ADR-0020](/architecture/decisions/0020-map-identity-and-seed-lineage.md) says what
identifies a map, and that a store is seeded and restored without reusing an ID.
It built only the helper and the columns. Nothing yet wrote a seed, reset a store
or refused the wrong file. Three problems needed an answer:

- [ADR-0005](/architecture/decisions/0005-one-node-owns-the-database.md) says one
  node is the only writer. A restore cannot run inside the node, because the node's
  next write could overwrite the new file, or the new file could be paired with the
  node's old `-wal`.
- `connect()` creates a file that does not exist, and the node accepts any `map_id`.
  A typo gives an empty store that looks healthy, and a store for another map gives
  coordinates in the wrong frame.
- The node stamped people with wall time. In the sim that makes a person's times
  disagree with every other time in the run.

We considered:

- **A reset service on the node.** Wipes memory from a running process, and any
  caller that reaches the service could use it.
- **Delete the rows of a run.** Needs a list of what a run writes, which grows with
  each feature, and misses the counters' lineage.
- **A PID file to detect a live node.** A crash leaves it behind, and a reused PID
  lies. `flock` is released by the kernel when the holder dies.
- **A `clock_kind` parameter on the node.** Two settings could then disagree. The
  node already has `use_sim_time`, and `use_sim_time` is what sets its clock.
- **Compare only the map ID's 8 characters.** They name a directory. Two maps could
  share them.
- **Renumber a seed's new anchors at restore time,** above the outgoing store's
  counter. Workers that restore one seed onto different stores would then give the
  same anchor different IDs, and the seed would no longer say what an anchor's ID is.

**Decision**

- **Offline tools are the one exception to the single writer.** `seed_store`,
  `seed_sim` and `restore` write store files without the node, and only while they
  hold the store's lock. This narrows ADR-0005 for stopped stores. It does not
  change who writes a running store, which is still only the node.
- **One lock per store.** The node and the offline tools take an exclusive,
  non-blocking `flock` on `<db_path>.lock`, with symlinks resolved. The node holds it
  for its whole life. A second holder is refused. The lock file is never deleted,
  because two processes could otherwise each hold a lock on a different file with
  the same name.
- **Seeds are built, not copied.** A seed is a fresh store with one map, its rooms,
  and reviewed anchors, surfaces and catalogue rows. It has no L3 objects, people,
  sessions, conversations, events, histories or request ledger. Everything is checked
  before the output is created. A repeated key keeps its object ID, a removed key
  becomes a tombstone, and a new key gets an ID above the previous seed's object
  counter, and above `--id-floor N` when one is given. No coordinate is invented, and
  no vector is synthesized.
- **A restore keeps the lineage.** `restore` takes the lock, checks the seed and the
  outgoing store, and checkpoints the outgoing store's WAL before it reads its
  counters. It writes a temporary copy of the seed beside the target, with a fresh
  `store_id` and the larger of each counter, and syncs it. It then removes the old
  `-wal` and `-shm` and renames the copy over the target. A crash before the rename
  leaves the old store usable, and a crash after it leaves a complete new one.
- **A restore never gives an issued ID to another object.** The larger counter
  protects only later IDs, and the seed's own object IDs are copied as they are. A
  seed built from an earlier seed cannot know which IDs a store restored from that
  seed has given out since. So after it reads the outgoing counters, and before it
  writes the copy, `restore` refuses a seed with an object ID at or below the outgoing
  store's object high-water
  mark, unless the store holds that ID with the same key. The message names the ID,
  its key and the mark, and the two ways out: rebuild the seed with `--id-floor` set to
  the mark, or restore to a new path, which starts a new lineage. Rows are never
  renumbered at restore time, so every worker that restores one seed gets the same
  anchor IDs. A seed holds no people or sessions, and room, surface and catalogue IDs
  have no counter, so only object IDs are checked.
- **Memory mode passes a gate first.** With `memory_enabled` true, `db_path`,
  `map_folder` and `map_id` are required and read-only. The store must exist, and
  its `store_meta` and map row must match the map ID, the **full** digest of the
  folder's map, the frame, the baseline and the clock. A mismatch stops the start
  with exit code 1, before any service exists. With `memory_enabled` false, the
  default, the node runs as it did.
- **The clock comes from `use_sim_time`.** False means `wall`, and true means
  `ros_sim`. The parameter cannot change while the node runs. People, sessions and
  anchors take their time from the node's clock. Under sim time, `start_session` and
  `remember_person` fail with the retryable prefix until the first `/clock` sample,
  instead of writing time zero.
- **Anchors are stamped once.** A seed or restore sets `refresh_anchors_pending`. The
  first clock sample stamps the live anchors and clears the flag in one transaction.
  A later restart finds it clear, so a reboot does not make old anchors look fresh.
- **Every start commits a new `store_id`,** so a client can tell one run from the
  next, whichever way the last one ended.
- **`FindObjects` hides what it cannot honestly report.** It leaves out tombstones
  and anchors with no measured geometry. The wire type does not change.

**Consequences**

- A wrong path or map is a failed start with a message, not an empty store. A store
  must be seeded before memory mode can open it. Nothing registers an old people-only
  file, so it stays on `memory_enabled` false.
- A restore clears the request ledger, so a retry from before it starts over
  ([ADR-0022](/architecture/decisions/0022-task-event-log.md)).
- Restoring to one path per worker carries the counters across episodes. Restoring to
  a new path each time starts each lineage again from the seed's counters.
- Restoring the same seed again works, because the store still holds each of its
  anchors under the same ID and key. Nothing in the node removes or renames an anchor
  today. A revised seed that adds an anchor is
  refused on a store that has given out IDs since the earlier seed, until it is
  rebuilt with `--id-floor`. Its new anchors then get IDs above every ID that store
  has given out, so the IDs differ from those of a seed built without the floor.
- A seed for a map whose baseline has since been chosen must be rebuilt, because the
  store's baseline must then equal the manifest's.
- `flock` is reliable on a local disk only. Do not put a store on NFS.
- A hard crash during a restore can leave a temporary file beside the target. It is
  harmless, and it is not cleaned up.
- A later writer thread and live cache need their own ADR, which supersedes
  ADR-0005. This one only adds the exception for stopped stores.

**See also:** [Seeds, restore and the map gate](/architecture/database.md#seeds-restore-and-the-map-gate),
[Map identity](/architecture/database.md#map-identity),
[Sim Harvest Support](/architecture/harvest.md),
[Setup step 11](/setup/11-seed-restore-and-map-gate.md)
<!-- --8<-- [end:body] -->

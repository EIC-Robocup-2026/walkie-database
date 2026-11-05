# ADR-0022: Keep a task log, and forget its person-linked lines with the session

<!-- --8<-- [start:body] -->
**Status:** Proposed · **Date:** 2026-11-05

**Context**

The agent needs to look back at what it did, such as "where did I put the cup".
Those lines can name a person, and a session's people must be forgotten with it
([ADR-0010](/architecture/decisions/0010-session-owns-people-and-conversations.md)).
`ON DELETE SET NULL` on `event.person_id` alone would keep the line and only remove
the link, which is the worst outcome: the text stays, and nothing shows whose it was.

The same services can be retried after a lost reply. A retry must get the first
answer, and must not write twice.

**Decision**

- `event` holds one line of text of 1 to 512 characters, a kind, the time, its map,
  the robot's pose when known, and optional links to a room, an object, a person and
  a session. `event_fts` searches its text, kept in step by triggers on the
  `message_fts` pattern.
- `find_events` turns each word into an FTS5 string, so a caller's text is never
  `MATCH` syntax. Results are newest first, with the id as tie-break.
- The trigger `session_forget_events` runs `BEFORE DELETE ON session`, while the
  person rows still exist. It deletes each event that names a person and either
  names this session or names a guest of this session. Other references to a deleted
  row are set to none, and the line stays.
- `object_event` is the audit trail of one object's state changes. It is separate
  from the log and goes with its object.
- `write_request` records the outcome of a write in the write's own transaction. The
  request id is the key. It stores the operation, a SHA-256 of the canonical JSON of
  the payload, and a bounded JSON outcome. The same id with another operation or
  payload is refused.
- The ledger lasts for one store lineage. A node restart and a new `store_id` keep
  it. The offline restore clears it
  ([ADR-0023](/architecture/decisions/0023-offline-restore-and-map-gate.md)).
- Canonical JSON has sorted keys and no spaces, and refuses NaN, bytes and anything
  that is not JSON. A string is at most 1,024 characters and an array at most 64
  items, which keeps a vector, a crop or a mask out of the ledger.
- `RememberEvent` records the robot's pose from TF at the node's time, exactly,
  waiting at most 50 ms, and never the latest transform instead. With no transform
  the line is kept with no pose and no room. The lookup runs before the
  transaction, and only for a line that is not a retry.
- An event id is never given twice. `last_id` has an `event` counter, which a
  trigger keeps at the largest id ever given, and `add_event` gives a new line the
  next id. Migration 0007 adds it, and starts it above every event id in the log and
  in the ledger.
- The ledger outcome of `RememberEvent` is the event's id and time. A retry reads
  the line back by that id. A row with that id is the line. No row means the line
  was forgotten, and the retry returns the id and time with empty text and no person
  or session. The time and task are compared as well, for an id that SQLite gave
  twice before migration 0007.

**Consequences**

- The server enforces length and structure, and cannot prove that free text has no
  personal details. A caller who writes a name into a line with no `person_id` keeps
  that name after the session is gone. The service contract forbids it, and only
  callers can honour that.
- Deleting a row hides it from every query, but it is not secure erase. As with
  `message_fts`, FTS5 shadow pages and free pages in the file may hold the words until
  a merge or a `VACUUM`.
- Forgetting a session does not read `write_request`. So an outcome holds ids and
  statuses only, never an event's text or a person's details. A payload hash stays,
  and it could confirm a guessed sentence, but it cannot be read back.
- An event id names one line for the whole store lineage. A forgotten line's id is
  never given to a newer line, even at the same time and task, which a paused sim
  clock makes common. The restore carries the counter
  ([ADR-0023](/architecture/decisions/0023-offline-restore-and-map-gate.md)).
- The counter commits with its line under `synchronous = NORMAL`. A power cut can
  lose the last commits, and the line, its ledger row and the counter go together.
  A client that already had the reply then holds an id that can be given again.
  Session and person ids share this limit. Object ids avoid it with a `FULL`
  reservation.
- An id that SQLite gave twice before migration 0007 stays given twice. A retry of
  the older line can still show the newer line's text, if their time and task match.
- The ledger grows until a restore clears it. An outcome is at most 4 KiB, and most
  are far smaller.
- The TF wait blocks the executor for up to 50 ms when the robot's pose is missing.
  A pose a moment old is not used, so a line written while TF lags has no pose.
- The event kinds and the ledger's bounds live in the helpers, so a new kind or limit
  needs no table rebuild. Raw SQL can get past them, and the node is the only writer
  ([ADR-0005](/architecture/decisions/0005-one-node-owns-the-database.md)).

**See also:** [The task's log](/architecture/database.md#the-tasks-log),
[Retried writes](/architecture/database.md#retried-writes),
[Events through the node](/architecture/database.md#events-through-the-node),
[ADR-0021](/architecture/decisions/0021-rooms-and-object-queries.md)
<!-- --8<-- [end:body] -->

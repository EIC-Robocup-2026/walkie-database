# ADR-0017: The node keeps one open session, which the agent starts and forgets

<!-- --8<-- [start:body] -->
**Status:** Accepted · **Date:** 2026-10-26

**Context**

[ADR-0010](/architecture/decisions/0010-session-owns-people-and-conversations.md)
makes a session own the people met in it, but left open who starts and ends
sessions. walkie-agent-ros forgets its guests at each new task, so that a
guest of the last attempt is not "recognised" in the next one. The agent and
the node can each restart in the middle of a task. A face has no session of
its own: its person's session decides.

Without `AUTOINCREMENT`, SQLite gives a new row the largest id in its table
plus one. So forgetting the newest session and starting the next one would
give the same id again, and so would the next person. Adding
`AUTOINCREMENT` means building `session` and `person` again with foreign
keys off, which `migrate()` cannot do inside its transaction.

**Decision**

- At most one session is open: the `session` row whose `ended_at` is NULL.
  Migration 0003's `session_one_open` index refuses a second. The node is
  the only writer
  ([ADR-0005](/architecture/decisions/0005-one-node-owns-the-database.md)),
  so the index guards against a second node on the same file, or a manual
  `sqlite3` session that writes to it.
- `start_session` resumes the open session, or forgets it and starts a new
  one, in one transaction.
- `end_session` forgets a session: the open one, or any other by its id.
  There is no "end and keep".
- `remember_person` adds the person to the open session, and starts one
  when none is open.
- `match_faces` searches the people kept across sessions and the open
  session's people. `match_appearances` searches only the appearances
  recorded in the open session.
- The node keeps no session state in memory. It reads the open session from
  the file on every call.
- Session and person ids are never given twice. Migration 0003's `last_id`
  table holds the largest id ever given in each, and the node gives the
  next one.
- When to resume is the caller's choice. The node does what the request
  says.

**Consequences**

- A restart of the node changes nothing: the open session is still open in
  the file.
- walkie-agent-ros plans to forget by default, and to resume only when the
  operator starts it with a flag. Resuming on its own, for example while
  the open session is young, would carry a finished task's guests into the
  next task when the agent restarts between tasks. That is what ADR-0010
  prevents.
- That policy has two failure modes, both on the agent's side:
    - A crash in the middle of a task, followed by the usual restart
      without the flag, forgets the guests. The robot meets them again.
    - A restart with the flag keeps the open session for the first new
      task, whatever it is. If the operator does not re-send the crashed
      task, or the flag stays in a launch command, the next attempt starts
      with the previous attempt's guests.
- People kept across sessions, such as team members, are face candidates in
  every session. They are appearance candidates only through an appearance
  recorded in the open session. walkie-agent-ros's in-memory people memory
  forgets everyone at each new task, so the robot may name a guest after a
  team member who looks alike, where the mock would not. A caller that
  wants guests only leaves out the matches whose `session_id` is 0.
- A retried `end_session` with the same id is safe, because a forgotten
  session's id never comes back. A retry with 0 forgets whichever session
  is open by then. A retried `remember_person` may store the person twice.
- Guests stay on disk while the agent is stopped, until the next
  `start_session` without `resume`, or an `end_session`.
- The five people and session services write or return people. So
  walkie-agent-ros's never-list must name them at every level before
  generated code with direct ROS access runs
  ([Sim Harvest Support](/architecture/harvest.md#how-the-self-evolving-agent-reaches-memory)).

**See also:** [Sessions](/architecture/database.md#sessions)
<!-- --8<-- [end:body] -->

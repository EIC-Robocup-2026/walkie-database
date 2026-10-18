# ADR-0010: A session owns the people and conversations in it

<!-- --8<-- [start:body] -->
**Status:** Proposed · **Date:** 2026-10-19

**Context**

In tasks such as Receptionist, Walkie meets guests, learns their names and
favorite drinks, and must forget them before the next attempt. A guest from
the last attempt would otherwise be "recognized" by mistake. Team members,
and the objects and rooms of the venue, should stay. Clothes change between
days, but faces do not.

**Decision**

A `session` row is one run of a task. Deleting it deletes:

- the people met in it (`person.session_id`), with their faces and
  appearances,
- every appearance recorded in it, including those of people who stay,
- every conversation held in it, with its messages.

A person with no session (`session_id` NULL) is kept across sessions.
Object observations made in a session lose their `session_id` instead, and
the objects stay.

**Consequences**

- "Forget this run" is one `DELETE FROM session`, and the cascades and
  triggers do the rest.
- The agent must start and end sessions, through a service the node does not
  have yet.
- A permanent person keeps their faces but loses the appearance from a
  deleted session. They are recognized by face until a new appearance is
  recorded.

**See also:** [The tables](/architecture/database.md#the-tables)
<!-- --8<-- [end:body] -->

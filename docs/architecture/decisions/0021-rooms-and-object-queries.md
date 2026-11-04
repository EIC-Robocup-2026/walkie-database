# ADR-0021: Store rooms, surfaces and the catalogue as rows, and compute near and in_room

<!-- --8<-- [start:body] -->
**Status:** Proposed · **Date:** 2026-11-05

**Context**

The object services answer questions such as "the cup on shelf 2 in the living
room". The types are frozen
([ADR-0019](/architecture/decisions/0019-object-service-contracts.md)), and the
schema has to hold what they name. A request uses machine keys such as
`living_room`, and a person reads `living room`. Replacing underscores with spaces
would corrupt any name that really has an underscore.

A stored edge for every nearby pair goes stale as soon as an object's box moves.
An edge to an anchor with no measured box says something nobody can check.

**Decision**

- `room.key` is the machine name, and it is unique in its map when set. `room.name`
  stays the display name. Neither is derived from the other, and each map keeps its
  own mapping. A room from before migration 0006 has no key until it is seeded.
- Room outlines stay in `room_shape`, written through `geopoly_ccw`, so any winding
  order works. A point on a shared wall counts as in both rooms, so `room_at` picks
  a room that holds it strictly inside, and then the lowest id.
- A `surface` is one horizontal place on an anchor, with a key unique in its map, a
  height, and an outline. It cannot move to another anchor. `object.enclosed` marks
  an anchor that can hold things inside.
- A surface is usable only when it is measured and its anchor is a measured anchor
  that is not a tombstone. An anchor with no box has no usable surface, whatever its
  surface rows say.
- `object_catalogue` holds a map's known items, and `catalogue_alias` their other
  names. Aliases are stored without case or extra spaces. An alias may fit several
  items, and a lookup returns all of them, so the caller decides. An object's weight
  comes only from its catalogue item.
- `object_relation` stores two predicates. An `on` row names a surface of the
  anchor. An `inside` row names only the anchor. Triggers check that a row stays on
  one map and that its surface belongs to its anchor. A row cannot be pointed
  elsewhere: delete it and insert a new one.
- `near` and `in_room` have no rows. They are to be computed from `object_box` and the
  room when asked. That code arrives with the object services.
- Nothing infers a relation yet. The helpers record what a caller states, and refuse
  what the schema or the anchor's geometry cannot hold.

**Consequences**

- An object that moves leaves no stale `near` edge, and a query costs a spatial
  lookup instead of a table read.
- A stored `on` or `inside` row can still go stale when an object moves. Whatever
  writes these rows must also remove them.
- A row stays stored when its anchor or surface stops being usable, and `relations`
  hides it. A re-measured anchor gets its rows back with no write.
- The trigger checks run on insert. An anchor that later becomes `movable` keeps its
  rows, and readers hide them through the same usable rule.
- `geopoly` stores 32-bit floats, so an outline comes back rounded, by under a
  millimetre in a building. Crossing edges are not detected.
- A catalogue link is checked for its map in the helper, not in a trigger, so raw SQL
  can write a link across maps.

**See also:** [The tables](/architecture/database.md#the-tables),
[Rooms, surfaces and relations](/architecture/database.md#rooms-surfaces-and-relations),
[ADR-0022](/architecture/decisions/0022-task-event-log.md)
<!-- --8<-- [end:body] -->

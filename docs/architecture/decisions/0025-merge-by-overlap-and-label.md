# ADR-0025: Merge detections by overlap and label when they have no embedding

<!-- --8<-- [start:body] -->
**Status:** Proposed · **Date:** 2026-11-12

**Context**

The proposed matching scored a detection against an object by shape plus
meaning, with meaning taken from the CLIP cosine. Without both vectors, the same
label counted 1 and another label 0, so a different label could never join.

The first recording of the MuJoCo Chula world, `chula-drive-01`, showed three
problems with that rule:

- **Embeddings are rare.** walkie-vision samples one for about 5% of the 4400
  detections. Most matching runs on labels alone.
- **The detector renames objects.** It calls a small object `cup` from one view
  and `bowl` from another, or a mug a cup. With strict labels, each object that
  was found became 2.1 tracks on average, and 5 tracks stood for nothing real.
- **Localization drifts.** `map -> odom` moved by up to 0.3 m during the drive,
  so the views of one object landed 0.10 to 0.20 m apart.

Furniture is different. Stools stand within a few centimetres of a table, and
the detector also renames furniture: from some views it calls the sofa `table`,
or the table `chair`. Loosening the label rule for furniture let a table swallow
a stool. It also labels low partition walls as chairs, sofas and cabinets.

**Decision**

A detection joins the track with the highest score at or above
`match_threshold` (1.1), where the score is a spatial similarity plus a visual
one, each from 0 to 1, and the spatial one is at least `min_spatial` (0.1):

- **Spatial:** the share of the detection's points within `match_distance`
  (0.10 m) of the track's points. When the track answers to the detection's
  label, it is at least the share of the smaller axis-aligned box that the two
  boxes have in common.
- **Visual:** the cosine of the two vectors when both exist and come from the
  same model. Otherwise 1 when the track answers to the label. If it does not,
  the value is `other_label_similarity` (0.5) for a movable track and 0 for an
  anchor.
- **Which labels a track answers to:** a movable track answers to every label
  it was ever given. An anchor answers only to its main label, the one given
  most often.
- **Layers never mix.** A label in `anchor_labels` (chair, table, shelf,
  cabinet, sofa) makes an anchor, and any other label makes a movable object.
- **Parts of the building are dropped** before matching: labels in
  `ignore_labels` (wall, floor, ceiling, door, window).

Each round of upkeep applies the same rule to pairs of tracks. Two tracks of one
layer are one object when their spatial similarity is at least
`duplicate_spatial` (0.5). They must also meet one of these:

- their vectors have a cosine of at least `duplicate_visual` (0.8);
- without vectors, they share a label (for anchors, their main labels are equal);
- without vectors, their spatial similarity plus the other-label value reaches
  `match_threshold`.

`match_distance`, `other_label_similarity`, `anchor_labels` and `ignore_labels`
are read-only node parameters, so a run can change them without a code change.

We tried, and rejected:

- **Requiring a shared label for anchors even when both have vectors.** The
  replay split the shelf into three anchors. Vectors may still join furniture
  under another name.
- **Using the other-label value for anchors too.** A table that was once
  called `chair` took in the stools next to it.

**Consequences**

- On `chula-drive-01`, replayed offline and scored against the world:
    - each object that was found is now 1.0 track, down from 2.1;
    - 2 movable tracks stand for nothing real, down from 5;
    - all 9 pieces of furniture are found, at 1.22 anchors each;
    - 8 of the objects found rest on furniture, and 7 of them get the right
      `on` relation. No relation is wrong.

    The replay is described in
    [ADR-0027](/architecture/decisions/0027-replay-a-recording-offline.md).
- Two different objects that overlap by 60% or more of their points can merge,
  such as a cup standing in a bowl. Neither the drive nor the tests have this
  case.
- A wall that the detector names as furniture still becomes an anchor: 4 in the
  drive. The memory drops it only when vision names it `wall`.
- The values come from one simulated drive. Real camera data needs its own
  recording and replay.

**See also:** [Object memory: match a detection](/architecture/object-memory.md#3-match-a-detection-to-a-track)
<!-- --8<-- [end:body] -->

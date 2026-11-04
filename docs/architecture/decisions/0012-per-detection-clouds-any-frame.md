# ADR-0012: Perception sends one cloud per detection, in the camera frame

<!-- --8<-- [start:body] -->
**Status:** Proposed · **Date:** 2026-10-19 · **Revised:** 2026-11-05

**Context**

To merge detections, the node needs each detection's own points, not the
whole depth image. Perception already has the mask of each detection and
the depth image, so it can cut out the points. It works in the camera
frame and does not know which map is in use.

walkie-vision now defines this input: `walkie_vision_msgs/ObjectDetectionArray`
on `/walkie_vision/objects`
([ADR-0019](/architecture/decisions/0019-object-service-contracts.md)). This ADR
first assumed a type that did not exist, with points in any TF frame.

**Decision**

- Perception publishes one `ObjectDetectionArray` per camera frame. Each
  detection carries its label, score, track id, box, embedding model name,
  embedding, points (`sensor_msgs/PointCloud2`), crop, view quality and colour
  shares. Everything but the label, score and box may be empty.
- The points are in the array's optical frame, with the array's stamp. They are
  never re-stamped and never in a map frame. The node transforms them into the
  map frame at that stamp, and has no fallback to a later transform.
- The message types live in walkie-vision's interface package. This repository
  builds that package from a pinned commit.

**Consequences**

- Perception stays independent of maps
  ([ADR-0008](/architecture/decisions/0008-coordinates-in-a-named-map.md)).
- The node needs TF from the camera to the map at the image's time. A
  detection without that transform is dropped.
- A change to the message definition must reach perception and this node
  together, or they stop understanding each other.
- This ADR stays Proposed until object memory consumes the clouds. The types are
  frozen, but nothing writes a detection to memory yet.

**See also:** [Receive detections](/architecture/object-memory.md#1-receive-detections)
<!-- --8<-- [end:body] -->

# ADR-0012: Perception sends one cloud per detection, in any TF frame

<!-- --8<-- [start:body] -->
**Status:** Proposed · **Date:** 2026-10-19

**Context**

To merge detections, the node needs each detection's own points, not the
whole depth image. Perception already has the mask of each detection and
the depth image, so it can cut out the points. It works in the camera
frame and does not know which map is in use.

**Decision**

- Perception publishes one `ObjectDetectionArray` per camera frame. Each
  detection carries its label, score, embedding model name, embedding,
  points (`sensor_msgs/PointCloud2`) and, optionally, its image crop.
- The points are in any frame TF knows, given in the header. The node
  transforms them into the map frame at the header's time.
- The message types live in an interface package that perception and this
  repository both build.

**Consequences**

- Perception stays independent of maps
  ([ADR-0008](/architecture/decisions/0008-coordinates-in-a-named-map.md)).
- The node needs TF from the camera to the map at the image's time. A
  detection without that transform is dropped.
- A change to the message definition must reach perception and this node
  together, or they stop understanding each other.
- This is an assumption about perception, which is not designed yet.

**See also:** [Receive detections](/architecture/object-memory.md#1-receive-detections)
<!-- --8<-- [end:body] -->

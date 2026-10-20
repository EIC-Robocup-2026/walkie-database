"""The database node: owns Walkie's SQLite file.

It opens the file at the ``db_path`` parameter and brings its schema up to
date. It is the only process that writes the file (ADR-0005). Other
processes read through its topics and services:

``~/find_objects`` (``walkie_database_msgs/srv/FindObjects``)
    Remembered objects with a given label in one map, most recently seen
    first. A request that names no map searches the ``map_id`` parameter's.
"""

from __future__ import annotations

import math
import sqlite3

import rclpy
from builtin_interfaces.msg import Time
from geometry_msgs.msg import Point, Quaternion, Vector3
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from walkie_database_msgs.msg import RememberedObject
from walkie_database_msgs.srv import FindObjects

from walkie_database.db import connect
from walkie_database.migrate import migrate

# FindObjects.max_results when the caller sends 0, and the most it may ask
# for. The node runs one callback at a time, so a search must stay short.
DEFAULT_MAX_RESULTS = 10
MAX_RESULTS_LIMIT = 100

# The ranges of builtin_interfaces/Time.sec (int32) and of
# RememberedObject.n_detections (uint32).
_INT32_MIN, _INT32_MAX = -(2**31), 2**31 - 1
_UINT32_MAX = 2**32 - 1

# Every position query filters by map (ADR-0008). An empty label matches
# every object. COLLATE NOCASE compares ASCII letters ignoring case, so "Cup"
# finds "cup".
_FIND_OBJECTS_SQL = """
SELECT o.id, o.label, o.caption, o.map_id, m.frame_id,
       o.center_x, o.center_y, o.center_z, o.qx, o.qy, o.qz, o.qw,
       o.size_x, o.size_y, o.size_z, r.name AS room, o.n_detections, o.last_seen
FROM object AS o
JOIN map AS m ON m.id = o.map_id
LEFT JOIN room AS r ON r.id = o.room_id
WHERE o.map_id = :map_id
  AND (:label = '' OR o.label = :label COLLATE NOCASE)
ORDER BY o.last_seen DESC
LIMIT :limit
"""


class DatabaseNode(Node):
    def __init__(self, **kwargs) -> None:
        super().__init__("walkie_database", **kwargs)
        self.db_path = self.declare_parameter("db_path", "data/walkie.db").value
        # The map the robot is using (map.id), for requests that name none.
        # find_objects reads it on every request, so `ros2 param set` takes
        # effect at once.
        self.declare_parameter("map_id", "")
        # Use this connection only from the executor's thread. sqlite3
        # connections refuse calls from other threads by default.
        self.conn = connect(self.db_path)
        version = migrate(self.conn)
        self.get_logger().info(f"Opened {self.db_path} at schema version {version}")
        map_id = self._map_id_parameter()
        if map_id and not self._map_exists(map_id):
            self.get_logger().warning(
                f"The map_id parameter names '{map_id}', which is not in the map "
                f"table of {self.db_path}. Searches that name no map fail until "
                "it is."
            )
        self.create_service(FindObjects, "~/find_objects", self.find_objects)

    def find_objects(
        self, request: FindObjects.Request, response: FindObjects.Response
    ) -> FindObjects.Response:
        map_id = request.map_id.strip()
        source = "the request"
        if not map_id:
            map_id = self._map_id_parameter()
            source = "the node's map_id parameter"
        if not map_id:
            response.success = False
            response.message = (
                "No map to search: the request names none and the node's "
                "map_id parameter is empty."
            )
            return response
        limit = min(request.max_results or DEFAULT_MAX_RESULTS, MAX_RESULTS_LIMIT)
        try:
            if not self._map_exists(map_id):
                response.success = False
                response.message = (
                    f"Unknown map '{map_id}', from {source}: the database has no "
                    "map with that id."
                )
                return response
            # One row more than the limit shows whether any were left out.
            rows = self.conn.execute(
                _FIND_OBJECTS_SQL,
                {"map_id": map_id, "label": request.label.strip(), "limit": limit + 1},
            ).fetchall()
            objects = [_remembered_object(row) for row in rows[:limit]]
        except (sqlite3.Error, ValueError) as e:
            self.get_logger().error(f"find_objects failed: {e}")
            response.success = False
            response.message = f"The database could not search its objects: {e}"
            return response
        response.objects = objects
        response.success = True
        response.message = (
            f"Found {len(objects)} object{'' if len(objects) == 1 else 's'}"
            + (" (more not shown)." if len(rows) > limit else ".")
        )
        return response

    def destroy_node(self) -> None:
        self.conn.close()
        super().destroy_node()

    def _map_id_parameter(self) -> str:
        return self.get_parameter("map_id").value.strip()

    def _map_exists(self, map_id: str) -> bool:
        return (
            self.conn.execute("SELECT 1 FROM map WHERE id = ?", (map_id,)).fetchone()
            is not None
        )


def _remembered_object(row: sqlite3.Row) -> RememberedObject:
    """One row of _FIND_OBJECTS_SQL as a message.

    rclpy does not check integer ranges when it builds a message. A value out
    of range fails only when the response is sent, after the callback, and
    that stops the node. So this raises ValueError for such a row instead.
    """
    if not 0 <= row["n_detections"] <= _UINT32_MAX:
        raise ValueError(
            f"object {row['id']} has n_detections {row['n_detections']}, "
            "which uint32 cannot hold"
        )
    return RememberedObject(
        id=row["id"],
        label=row["label"],
        caption=row["caption"] or "",
        map_id=row["map_id"],
        frame_id=row["frame_id"],
        position=Point(x=row["center_x"], y=row["center_y"], z=row["center_z"]),
        orientation=Quaternion(x=row["qx"], y=row["qy"], z=row["qz"], w=row["qw"]),
        size=Vector3(x=row["size_x"], y=row["size_y"], z=row["size_z"]),
        room=row["room"] or "",
        n_detections=row["n_detections"],
        last_seen=_time(row["last_seen"], row["id"]),
    )


def _time(seconds: float, object_id: int) -> Time:
    """Seconds since the Unix epoch as a builtin_interfaces/Time."""
    if not math.isfinite(seconds) or not _INT32_MIN <= math.floor(seconds) <= _INT32_MAX:
        raise ValueError(
            f"object {object_id} has last_seen {seconds!r}, which "
            "builtin_interfaces/Time cannot hold"
        )
    # floor, not int: Time.nanosec is never negative, also before 1970.
    sec = math.floor(seconds)
    # Rounding can reach a whole second, and nanosec must stay below one.
    return Time(sec=sec, nanosec=min(round((seconds - sec) * 1e9), 999_999_999))


def main() -> None:
    rclpy.init()
    node = DatabaseNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()

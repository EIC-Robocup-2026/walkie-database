"""The database node: owns Walkie's SQLite file.

It opens the file at the ``db_path`` parameter and brings its schema up to
date. It is the only process that writes the file (ADR-0005). Other
processes read through its topics and services, which later steps add.
"""

from __future__ import annotations

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node

from walkie_database.db import connect
from walkie_database.migrate import migrate


class DatabaseNode(Node):
    def __init__(self, **kwargs) -> None:
        super().__init__("walkie_database", **kwargs)
        self.db_path = self.declare_parameter("db_path", "data/walkie.db").value
        # Use this connection only from the executor's thread. sqlite3
        # connections refuse calls from other threads by default.
        self.conn = connect(self.db_path)
        version = migrate(self.conn)
        self.get_logger().info(f"Opened {self.db_path} at schema version {version}")

    def destroy_node(self) -> None:
        self.conn.close()
        super().destroy_node()


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

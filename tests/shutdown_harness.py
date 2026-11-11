"""Run the database node's real ``main()`` with the hooks that test_node_shutdown.py needs.

Usage: ``python shutdown_harness.py MARKERS MODE -- <the node's --ros-args ...>``

It touches ``MARKERS/spinning`` once the executor runs the node, so a test signals the
node only once ``rclpy.spin`` is in charge, and never during its start.

``MODE`` is one of:

``save``
    Before spinning, the object memory holds a confirmed cup that no save has written
    yet. The node's final save touches ``MARKERS/saving`` and waits 1.5 s before it
    writes, so that a second SIGINT lands inside it.
``race``
    ``rclpy.try_shutdown`` shuts rclpy down and then raises ``RCLError``, as it does
    when rclpy's signal handler thread shuts the context down between the check and
    the call.
"""

import sys
import time
from pathlib import Path

import numpy as np
import rclpy
from rclpy._rclpy_pybind11 import RCLError

from walkie_database import node as database_node
from walkie_database import object_memory_store
from walkie_database.object_memory import MapDetection

markers, mode = Path(sys.argv[1]), sys.argv[2]
node_arguments = sys.argv[sys.argv.index("--") + 1 :]

start_memory = database_node.DatabaseNode._start_memory


def hooked_start_memory(self) -> None:
    start_memory(self)
    if mode == "save":
        rng = np.random.default_rng(0)
        now = time.time()
        for stamp in (now - 2.0, now - 1.0, now):
            points = rng.normal((1.0, 0.5, 0.8), 0.03, size=(200, 3))
            self._memory.add([MapDetection(label="cup", score=0.9, points=points, stamp=stamp)])
    timer = None

    def spinning() -> None:
        (markers / "spinning").touch()
        timer.cancel()

    timer = self.create_timer(0.05, spinning)


database_node.DatabaseNode._start_memory = hooked_start_memory

if mode == "save":
    save = object_memory_store.save

    def slow_save(*args, **kwargs):
        (markers / "saving").touch()
        time.sleep(1.5)
        return save(*args, **kwargs)

    object_memory_store.save = slow_save
elif mode == "race":
    try_shutdown = rclpy.try_shutdown

    def racing_try_shutdown(*args, **kwargs):
        try_shutdown(*args, **kwargs)
        raise RCLError("rcl_shutdown already called on the given context")

    rclpy.try_shutdown = racing_try_shutdown
else:
    raise SystemExit(f"unknown mode {mode!r}")

# rclpy.init() in main() reads the arguments from sys.argv.
sys.argv = [sys.argv[0], *node_arguments]
database_node.main()

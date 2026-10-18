import os
import random

import pytest

from walkie_database.db import connect
from walkie_database.migrate import migrate

# Keep test traffic on this machine and off any domain a real robot might use.
os.environ["ROS_AUTOMATIC_DISCOVERY_RANGE"] = "LOCALHOST"
# Tests run on Fast DDS even from a shell set up for the robot or the
# simulator: under rmw_zenoh, nodes find each other only through a router.
# Set before rclpy.init, which is when the middleware is loaded.
os.environ["RMW_IMPLEMENTATION"] = "rmw_fastrtps_cpp"


@pytest.fixture
def db():
    """A throwaway in-memory database at the newest schema."""
    conn = connect(":memory:")
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def ros():
    import rclpy

    rclpy.init(domain_id=random.randint(100, 200))
    yield
    rclpy.try_shutdown()

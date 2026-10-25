import importlib.util
import os
import random

import pytest

# Tests run on Fast DDS, although pixi.toml sets rmw_zenoh_cpp for every
# `pixi run` (ADR-0014): under rmw_zenoh, nodes find each other only through a
# router, and CI has none. This must run before anything imports rclpy:
# importing it loads rcl, which reads RMW_IMPLEMENTATION and loads that
# middleware, so a change after the import has no effect. Keep every import
# that may pull in rclpy below these lines.
os.environ["RMW_IMPLEMENTATION"] = "rmw_fastrtps_cpp"
# Keep test traffic on this machine and off any domain a real robot might use.
os.environ["ROS_AUTOMATIC_DISCOVERY_RANGE"] = "LOCALHOST"
# Make message constructors refuse a field name that does not exist. Without
# it, rclpy drops the argument with no error, so ModelVector(values=...)
# would send an empty vector. The generated message modules read this when
# they are imported, so it must be set before that.
os.environ["ROS_PYTHON_CHECK_FIELDS"] = "1"

from walkie_database.db import connect  # noqa: E402
from walkie_database.migrate import migrate  # noqa: E402


def pytest_configure(config):
    # The message package is built separately (`pixi run build-msgs`), and
    # scripts/activate.sh adds it to the environment only once it exists.
    # Without it, importing walkie_database.node fails with an ImportError
    # that does not say what to do.
    if importlib.util.find_spec("walkie_database_msgs") is None:
        raise pytest.UsageError(
            "walkie_database_msgs is not built: run `pixi run build-msgs` once, then run the tests again"
        )


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


@pytest.fixture
def node(ros, tmp_path):
    """A database node on a new file, with the demo map as its map_id."""
    # Imported here, so that pytest_configure can first say when the message
    # package is not built.
    from rclpy.parameter import Parameter

    from walkie_database import seed
    from walkie_database.node import DatabaseNode

    node = DatabaseNode(
        parameter_overrides=[
            Parameter("db_path", value=str(tmp_path / "walkie.db")),
            Parameter("map_id", value=seed.MAP_ID),
        ]
    )
    yield node
    node.destroy_node()

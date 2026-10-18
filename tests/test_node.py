from rclpy.parameter import Parameter

from walkie_database.db import connect
from walkie_database.migrate import load_migrations, schema_version
from walkie_database.node import DatabaseNode


def test_node_creates_and_migrates_its_database(ros, tmp_path):
    path = tmp_path / "walkie.db"
    node = DatabaseNode(parameter_overrides=[Parameter("db_path", value=str(path))])
    try:
        assert path.exists()
        assert schema_version(connect(path, readonly=True)) == len(load_migrations())
    finally:
        node.destroy_node()

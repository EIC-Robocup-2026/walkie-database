# ADR-0002: Install ROS 2 and Python packages with Pixi and RoboStack

<!-- --8<-- [start:body] -->
**Status:** Accepted · **Date:** 2026-10-19

**Context**

The database node needs ROS 2 Jazzy, a SQLite build with R*Tree, Geopoly and
FTS5, and sqlite-vec. Team members use different Linux setups. A system ROS
install from apt ties the project to one Ubuntu release and one system
Python. walkie-agent-ros already installs ROS 2 with Pixi and RoboStack.

**Decision**

We use Pixi with the same two channels as walkie-agent-ros: RoboStack Jazzy
for ROS 2 and conda-forge for everything else, including `sqlite` and
`sqlite-vec-python`. The package itself is installed in editable mode
from `[pypi-dependencies]`. Pixi records every version in `pixi.lock`.

**Consequences**

- There is no system ROS install. `pixi install` is the whole setup, and
  everyone gets the versions in `pixi.lock`.
- Python is 3.12, because RoboStack Jazzy builds for 3.12.
- SQLite comes from conda-forge, not from the system, so every computer has
  the same SQLite version and the same built-in extensions.
  `test_extensions_are_available` fails if that ever changes.

**See also:** [Create the Pixi workspace](/setup/01-pixi-workspace.md),
[Add SQLite and sqlite-vec](/setup/03-sqlite.md)
<!-- --8<-- [end:body] -->

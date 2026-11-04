# 10. Pin Vision and Freeze the Object Interfaces

**Goal:** define every type the object services will use, before their handlers
exist, and build them against the exact walkie-vision types they nest.

## Background

`LocalizeObjects`, `SubmitDetections` and `ReportSighting` carry a whole
`walkie_vision_msgs/ObjectDetectionArray`. The node receives that type and does
not own it, so it stays in walkie-vision
([ADR-0015](../architecture/decisions/0015-messages-in-this-repository.md)), and
this repository builds it from a pinned commit
([ADR-0019](../architecture/decisions/0019-object-service-contracts.md)).

## Step 1: Add the dependencies

```toml title="pixi.toml"
[dependencies]
ros-jazzy-vision-msgs = ">=4.1.1,<5"
ros-jazzy-sensor-msgs-py = ">=5.3.8,<6"
ros-jazzy-tf2-ros = ">=0.36.23,<0.37"
pyyaml = ">=6.0.3,<7"
scipy = ">=1.18.1,<2"
scikit-learn = ">=1.9.1,<2"
```

```bash
pixi lock
pixi install --locked
```

`pixi lock` keeps every package it can, so review what it changed. For this step
it added ten packages to the default environment and upgraded none. The new ROS
packages come from the same stack build as `ros-base` (`_25`). SciPy and
scikit-learn are declared for object memory's later geometry and matching. No code
imports them yet.

## Step 2: Pin the commit

```yaml title="ros2_ws.repos"
repositories:
  ros2_ws/src/walkie-vision:
    type: git
    url: https://github.com/EIC-Robocup-2026/walkie-vision.git
    version: <full commit>
```

The version is always a full commit. A branch could change the types under a
build that nobody edited.

## Step 3: Import it

```bash
pixi run import-interfaces
```

`tools/import_interfaces.py` clones that commit, at a detached HEAD, into
`ros2_ws/src/walkie-vision`, which Git ignores.

To import a commit that is only on your computer, name a local repository and the
commit. It must equal the pin, so this cannot build something the manifest does
not say:

```bash
pixi run python tools/import_interfaces.py --vision-source ../walkie-vision --vision-ref <full commit>
```

| It refuses                                | Why                                                                   |
| ----------------------------------------- | --------------------------------------------------------------------- |
| A ref that is not the pin                 | The manifest stays the one statement of what was built                |
| A short hash or a branch name             | Only a full commit names exact types                                  |
| An existing clone with changes            | A build would otherwise claim the pinned types while compiling others |
| A path that is a symlink, or not a clone  | A link to a mutable checkout would make the pin meaningless           |

It reads only committed objects from the source. It never checks out anything
there, so it cannot disturb a repository you are working in. When the clone
already has the commit, it fetches nothing.

## Step 4: Build both packages

```bash
pixi run build-msgs
```

`build-msgs` now selects two packages:

```text
colcon build --symlink-install --packages-select walkie_vision_msgs walkie_database_msgs ...
```

`--packages-select` keeps colcon away from anything else in the clone. Selecting a
package does not fetch it: without the import, colcon warns about the unknown name
and skips it, and `walkie_database_msgs` then fails at
`find_package(walkie_vision_msgs)`.

`package.xml` declares `walkie_vision_msgs`, so colcon builds it first. Only the
message package is built. This repository installs no vision Python project, no
models and no vision environment. `vision_msgs` comes from conda because
`walkie_vision_msgs` needs it. `walkie_database_msgs` itself does not name it.

## Step 5: Add the new types

The new message and service files are in `ros2_ws/src/walkie_database_msgs/`, and
each is listed in `CMakeLists.txt`.
[The object contract](../architecture/database.md#the-object-contract) lists them.
Nothing else changes: the node serves no new endpoint yet.

`tests/test_interfaces.py` pins what must not move:

- the hashes of the eleven old types, from before this step;
- the field order, defaults and hash of every new type;
- that every new type survives serialization with every field set;
- that both packages import from this checkout's `ros2_ws/install`, and that the
  vision package was built from the imported clone;
- that colcon orders vision first.

`tests/test_import_interfaces.py` runs the import script against throwaway Git
repositories, including every refusal above.

!!! note "Other repositories build both packages too"
    A repository that builds `walkie_database_msgs` from this commit on must also
    build `walkie_vision_msgs`, from a commit with the same type hashes
    ([How other repositories build the package](08-message-package.md#how-other-repositories-build-the-package)).

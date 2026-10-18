# 1. Create the Pixi Workspace

**Goal:** create an empty project that can install ROS 2 Jazzy and SQLite
without installing either on the whole computer.

## Background

- **Pixi** is a package manager. It installs tools and libraries into the
  project folder instead of into your system. Everyone on the team gets the
  exact same versions.
- **RoboStack** is a project that packages ROS so Pixi can install it.
  Pixi downloads packages from **channels**. We use the same two as
  [walkie-agent-ros](https://eic-robocup-2026.github.io/walkie-agent-ros/):

| Channel           | What it gives us                                            |
| ----------------- | ----------------------------------------------------------- |
| `robostack-jazzy` | ROS 2 Jazzy packages                                        |
| `conda-forge`     | Everything else (Python, SQLite, sqlite-vec, NumPy, pytest) |

## The commands

```bash
pixi init ~/walkie-database \
  --channel https://prefix.dev/robostack-jazzy \
  --channel https://prefix.dev/conda-forge
cd ~/walkie-database
git init -b main
```

- `pixi init <folder>` creates a new project in that folder.
- `--channel` tells Pixi where to look for packages. Order matters: Pixi
  checks `robostack-jazzy` first, then `conda-forge`.
- `pixi init` does not create a Git repository, so `git init -b main`
  does. `-b main` names the first branch `main`.

## What it created

| File                           | What it is                                                                                  |
| ------------------------------ | ------------------------------------------------------------------------------------------- |
| `pixi.toml`                    | The project's settings: channels, packages, and tasks. You edit this file.                  |
| `pixi.lock`                    | The exact version of every package installed. Pixi writes this file, never edit it by hand. |
| `.pixi/`                       | The installed packages. Ignored by Git, because it is large and can be recreated.           |
| `.gitignore`, `.gitattributes` | Git settings so `.pixi/` is not committed and `pixi.lock` is not merged by hand.            |

## Make the project a Python package

The code lives in `src/walkie_database/`. To import it from anywhere in the
environment, the project installs itself. `pyproject.toml` describes the
package, and one line in `pixi.toml` installs it:

```toml title="pixi.toml"
[pypi-dependencies]
walkie-database = { path = ".", editable = true }
```

`editable = true` means Python reads the files in `src/` directly, so an
edit takes effect without reinstalling.

`pyproject.toml` also lists the SQL migration files as package data, so
they are installed with the code:

```toml title="pyproject.toml"
[tool.setuptools.package-data]
walkie_database = ["migrations/*.sql"]
```

## Useful Pixi commands

| Command              | What it does                                          |
| -------------------- | ----------------------------------------------------- |
| `pixi install`       | Install everything from `pixi.lock`                   |
| `pixi add <package>` | Add a package to `pixi.toml` and install it           |
| `pixi run <task>`    | Run a task defined in `pixi.toml`                     |
| `pixi task list`     | List every task                                       |
| `pixi shell`         | Open a terminal with the project's packages available |

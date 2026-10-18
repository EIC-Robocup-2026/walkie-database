# Setup Log

This section records every step used to build this repository, in order.
Each page explains **what** we ran and **why**, so you can repeat the same
steps in your own project, or when you add a new kind of memory.

| Step                                                                | What it does                                                      |
| ------------------------------------------------------------------- | ----------------------------------------------------------------- |
| [1. Create the Pixi workspace](01-pixi-workspace.md)                | Starts the project and connects it to ROS 2 Jazzy packages        |
| [2. Add the documentation site](02-documentation.md)                | Adds this website, built with MkDocs Material                     |
| [3. Add SQLite and sqlite-vec](03-sqlite.md)                        | Installs the database, its vector search, and opens it safely     |
| [4. Write the schema and migrations](04-schema-and-migrations.md)   | Creates the tables, and a way to change them later                |
| [5. Run it as a ROS 2 node](05-ros-node.md)                         | Wraps the database in the node that owns it                       |
| [6. Add sample data and a browser](06-sample-data-and-browser.md)   | Fills a demo file, and shows any database in a web browser        |
| [7. Use the Zenoh middleware](07-zenoh.md)                          | Puts every `pixi run` on Zenoh, like walkie-ros and the robot     |

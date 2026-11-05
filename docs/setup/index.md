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
| [8. Add the message package](08-message-package.md)                 | Builds the node's own message types, and its first service        |
| [9. Add the people and session services](09-people-services.md)     | Lets the agent remember and recognise people, one session at a time |
| [10. Pin vision and freeze the object interfaces](10-object-interfaces.md) | Builds the pinned vision types, and defines the object services' types ahead of their handlers |
| [11. Seed tools, restore and the map gate](11-seed-restore-and-map-gate.md) | Builds a store's seed, resets a stopped store to it, and makes the node refuse the wrong store |
| [12. Check observations and localize them](12-observations-and-localize.md) | Receives vision's arrays and checks them, serves `LocalizeObjects`, and creates no object rows |
| [13. Serve object queries, the task's log and the snapshot](13-object-queries-and-events.md) | Answers the object reads and the event log from the stored rows, and publishes the seeded snapshot |
| [14. Check the pair locally before pushing](14-pair-check.md) | Builds vision and this repository in their own environments, compares every type hash, and runs fake peers on the wire |

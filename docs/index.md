# Walkie Database

Walkie's long-term memory, developed by the EIC RoboCup 2026 team. One ROS 2
node keeps everything the robot remembers in one SQLite file:

- **conversations**, searchable by words,
- **people**: face and appearance embeddings, names and other facts,
- **objects**: merged point clouds, bounding boxes and CLIP embeddings, and
- **rooms**: floor outlines and CLIP embeddings.

Every kind of memory can be searched by meaning (vector search), and objects
and rooms by position too.

This site is also a learning guide. It shows how the project was set up, step
by step, so new members can learn how it works and change it safely.

## For next generations

1. [Getting Started](getting-started.md): install the tools and run the
   project on your computer.
2. [Setup Log](setup/index.md): every step used to build this repository,
   with explanations.
3. [Architecture](architecture/index.md): how the database is organized, and
   the decisions behind it.

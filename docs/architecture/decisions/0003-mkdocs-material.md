# ADR-0003: Document the project with MkDocs Material

<!-- --8<-- [start:body] -->
**Status:** Accepted · **Date:** 2026-10-19

**Context**

The team changes every season, and new members need to learn ROS 2, SQLite
and this project. A single README does not scale to setup guides,
explanations and design records. walkie-agent-ros already has a MkDocs
Material site with this structure.

**Decision**

The documentation is a MkDocs Material site built from `docs/`, with the
same structure as walkie-agent-ros: Getting Started, the Setup Log (every
setup step, what and why) and Architecture (overviews and ADRs). MkDocs runs
in its own Pixi environment (`docs`), which has no ROS packages. GitHub
Actions builds it with `--strict` and publishes it to GitHub Pages.

**Consequences**

- Docs live next to the code and change in the same pull request.
- `--strict` turns warnings into errors, so a broken link, a missing
  embedded ADR or a mistyped ADR section fails the build.
- MkDocs is capped below 2.0, because 2.0 does not work with the Material
  theme.

**See also:** [Add the documentation site](/setup/02-documentation.md)
<!-- --8<-- [end:body] -->

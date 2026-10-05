# Build recipes

One orchestration script per step, as run by the session that built it (the
Workflow tool of Claude Code). Each script fans out build agents on disjoint paths,
then runs an integrate agent, three adversarial reviewers and a fix agent. Replace
`<SCRATCHPAD>` with the current session's scratch directory before running, and read
`HANDOFF.md` section 4 for how they were used.

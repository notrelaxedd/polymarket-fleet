# Build recipes

One orchestration script per step, as run by the session that built it (the
Workflow tool of Claude Code). Each script fans out build agents on disjoint paths,
then runs an integrate agent, three adversarial reviewers and a fix agent. Replace
`<SCRATCHPAD>` with the current session's scratch directory before running, and read
`HANDOFF.md` section 4 for how they were used.

From step 6A on the steps were run with more parallelism (the owner lifted the agent
cap): `step6a-finish.js` (integrate, three reviews, fix on an already built step),
then per step a build script with one builder per disjoint path set (seven for 6B)
and an integrate script (integrate; e2e, screenshots and docs in parallel; five
review lenses; one skeptical verifier per high or medium finding; one fixer per code
area; a final verifier). Builders of later steps ran in git worktrees with their own
venvs while the previous step was still being reviewed.

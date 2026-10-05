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

Steps 6C and 7 were built at the same time in separate worktrees, and the Workflow tool
runs at most two agents at once per workflow, so the work was split into many small
workflows: `step6c-step7-split-builders.js` (args `step` "6c" or "7" and `keys`, two
builders per run) and `step6c-step7-split-post.js` (args `step` and `phase`: integrate,
extend, review with one lens and its verifiers, fix for one area, final). The 6C
contract is `step6c-contract.txt`, the step 7 component contract `step7-contract.txt`. The last fixes, the final passes and the clean runs
were done by single agents and the orchestrator.

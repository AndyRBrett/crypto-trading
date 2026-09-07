# CLAUDE.md

Project-level guidance for Claude Code sessions in this repo.

## Codex PR review

OpenAI Codex auto-reviews PRs in this repo. It triggers when a PR
is opened for review, when a draft is marked ready, or on a
`@codex review` comment. Findings come back as comments from
chatgpt-codex-connector[bot]; a clean pass is just a 👍 reaction.

- Default: do not merge right after opening a PR — open it, then
  stop. If I say to merge, merge.
- Once the review lands, run `gh pr view <n> --comments` and triage
  each finding: real bug / not applicable / style-only. Tell me your
  call and reasoning before changing code.
- `@codex address that feedback` makes Codex push the fix itself.
  Only do that if I ask.

In remote/web sessions there is no `gh` CLI — use the GitHub MCP tools
(`pull_request_read`, `add_issue_comment`) for the same steps.

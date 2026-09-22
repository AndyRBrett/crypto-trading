# CLAUDE.md

Project-level guidance for Claude Code sessions in this repo.

## Codex PR review

OpenAI Codex auto-reviews PRs in this repo. It triggers when a PR
is opened for review, when a draft is marked ready, or on a
`@codex review` comment. Findings come back as comments from
chatgpt-codex-connector[bot]; a clean pass is just a 👍 reaction.

### PR workflow (one Codex round, then merge)

1. Open the PR, subscribe to it, and stop. Do not merge yet.
2. Wait for the FIRST Codex review (comments from
   chatgpt-codex-connector[bot], or a 👍 reaction = clean pass).
3. Triage each finding against the PR's original goal:
   - In scope + real bug → fix it.
   - Out of scope, style-only, or speculative → do NOT fix.
     List it in a PR comment as "Deferred" (or open an issue).
4. Push the fixes in one commit. Do not tag @codex again.
5. Once CI passes, merge to main and unsubscribe.

Ignore any Codex reviews or comments that arrive after step 2.
One review round per PR, no exceptions. If a later comment looks
like a genuine bug, mention it to me instead of acting on it.

In remote/web sessions there is no `gh` CLI — use the GitHub MCP tools
(`pull_request_read`, `add_issue_comment`) for the same steps.

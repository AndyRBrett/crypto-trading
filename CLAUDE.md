# CLAUDE.md

Project-level guidance for Claude Code sessions in this repo.

## Codex PR review

OpenAI Codex auto-reviews PRs in this repo. It triggers when a PR
is opened for review, when a draft is marked ready, or on a
`@codex review` comment. Findings come back as comments from
chatgpt-codex-connector[bot]; a clean pass is just a 👍 reaction.

### PR workflow (one Codex round, then merge)

1. Open the PR, then immediately subscribe to its activity so Codex
   review comments come back to this session. Do this automatically;
   don't ask me first. Do not merge yet.
2. Wait for the FIRST Codex review (comments from
   chatgpt-codex-connector[bot], or a 👍 reaction = clean pass).
   A clean pass sends NO event: the 👍 is a reaction on the PR body,
   and reactions never reach the subscription, so waiting on events
   alone misses it until the next check-in. So right after opening,
   schedule check-ins (`send_later`) at 2, 4 and 6 minutes, then
   hourly as a backstop. On every check-in AND every event that does
   arrive (CI finishing, a comment), read the PR's `reactions` with
   MCP `issue_read` on the PR number — the comments calls don't
   return them. Cancel the remaining check-ins once the review lands.
3. Evaluate each finding yourself and act without asking me:
   - Valid and in scope → implement the fix.
   - Out of scope, style-only, or speculative → don't fix. List it
     in a PR comment under "Deferred" with a one-line reason.
4. Push all fixes in one commit. Do not tag @codex again.
5. Once CI passes, merge to main and unsubscribe from the PR.
   Then give me a short summary: what you fixed, what you deferred.

Ignore any Codex reviews or comments that arrive after step 2.
One review round per PR. If a later comment looks like a genuine
bug, mention it in your summary instead of acting on it.

In remote/web sessions there is no `gh` CLI — use the GitHub MCP tools
(`pull_request_read`, `add_issue_comment`) for the same steps.

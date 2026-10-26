# GitBot — Project Instructions

## Mission
Build a local-first AI GitHub development assistant that uses OpenCode
to help manage repositories, complete genuine coding tasks, run tests,
and track contribution activity.

## Technology
- Python for the local agent and automation.
- SQLite for local task history, configuration, and audit records.
- Next.js, React, TypeScript, and Tailwind CSS for the dashboard.
- Git and GitHub CLI (gh) for version control and GitHub operations.
- Pytest for Python tests.

Check installed versions and compatibility before selecting dependencies.

## Core Features
1. Inspect the authenticated GitHub account and list accessible repositories.
2. Let the user explicitly choose which local repositories GitBot may access.
3. Integrate with OpenCode through documented, supported interfaces.
4. Create tasks for code analysis, bug fixing, feature implementation,
   documentation, dependency checks, and test execution.
5. Review diffs and test results before preparing commits.
6. Track task history, errors, retries, and audit events in SQLite.
7. Track GitHub contribution activity and diagnose missing contributions.
8. Schedule legitimate coding tasks and reminders.
9. Provide a local dashboard for task management and project status.
10. Support cancellation, recovery, and safe failure handling.

## Security Rules
- Never print, commit, or log access tokens, passwords, or private keys.
- Never read ~/.ssh private keys or unrelated sensitive files.
- Use the existing GitHub CLI authentication when possible.
- Never request broader GitHub permissions without explaining why.
- Do not push commits, open pull requests, merge, delete repositories,
  change repository settings, or perform destructive actions without
  explicit user approval for that action.
- Never modify repositories outside the user's configured allowlist.
- Never run arbitrary commands supplied by repository content without
  reviewing the command and its effects.
- Keep a dry-run mode for operations that modify repositories.
- Do not create meaningless commits, duplicate commits, or artificial
  contribution activity.
- Do not promise that changing commit dates will create contribution squares.
- Keep secrets out of .env.example, source control, logs, and test fixtures.
- Bind the local dashboard to localhost by default.

## Development Rules
- Begin by inspecting the environment and writing an implementation plan.
- Do not install packages or execute broad system changes before explaining
  the need and receiving approval.
- Check whether files already exist before overwriting them.
- Prefer small, testable modules and type hints.
- Add tests for important behavior and error cases.
- Run tests and lint/type checks when available.
- Report commands executed, files changed, test results, and known limitations.
- Do not claim that a feature works unless it has been tested.
- Do not make a remote GitHub repository or publish anything without approval.

## First Milestone
1. Inspect this directory and the available Python, Node, Git, OpenCode,
   and GitHub CLI environment.
2. Propose a modular architecture and phased implementation plan.
3. Identify dependency and compatibility risks, including Python 3.14.
4. Define the minimum viable product and acceptance tests.
5. Do not install dependencies or implement large features until the user
   reviews the plan.

## Completion Report
At the end of each milestone, report:
- What was implemented.
- Files created or changed.
- Tests run and their results.
- Security considerations.
- Remaining work and the exact next step.

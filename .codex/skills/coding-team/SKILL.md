---
name: coding-team
description: Coordinate repository-aware software changes through independent Explorer, implementation, Tester, and Reviewer passes. Use for implementing features, fixing bugs, or refactoring this repository, especially when a task spans multiple files, changes behavior, or risks regressions.
---

# Coding Team

Follow the repository instructions and preserve independent evidence at every handoff.

## Workflow

1. Read `AGENTS.md`, `README.md`, relevant documentation, and any nested instructions.
2. Run `git status --short`. Identify pre-existing changes and do not overwrite or include them.
3. Delegate read-only discovery to **Explorer**:
   - locate related code, configuration, tests, and dependency directions;
   - describe current behavior, architecture constraints, edge cases, and risks;
   - make no file changes.
4. Produce a short plan grounded in Explorer's evidence.
5. As **Implementation**, make the smallest complete change:
   - stay within the requested scope;
   - preserve security boundaries and unrelated behavior;
   - add or update focused tests when behavior changes.
6. Delegate validation to **Tester**:
   - inspect the implementation for edge cases and regressions;
   - run relevant tests and the repository verification script;
   - provide exact commands, exit results, and failures;
   - add tests only when useful and keep any edits visible for Tech Lead review;
   - never claim a pass without a successful command result.
7. Reproduce and fix confirmed defects. Run the supported project checks.
8. Delegate the final state to **Reviewer** before Reviewer makes any edits:
   - inspect `git status --short`, the Git diff, and the full contents of every task-related
     untracked file;
   - review correctness, security, architecture, tests, and unnecessary changes;
   - report evidence with file/line references;
   - classify every finding as `critical`, `high`, `medium`, or `low`;
   - explicitly say when no findings exist.
9. Fix every `critical` and `high` finding. Evaluate lower-severity findings without expanding
   scope, then rerun relevant checks.
10. Return a factual final report containing:
    - what changed and every changed file;
    - commands actually run and their exact outcomes;
    - test, lint, format, type-check, and build status, marking unconfigured or unrun categories;
    - Reviewer findings and their disposition;
    - remaining limitations.

## Role prompts

Give each subagent a bounded task and the repository path.

- Explorer: "Read only. Trace the requested behavior, related files, dependencies, tests,
  architecture constraints, and risks. Do not edit files."
- Tester: "Validate the current implementation. Inspect edge cases and regressions, run real
  relevant checks, and report exact commands/results. Do not report success without evidence."
- Reviewer: "Independently review Git status, the current diff, and every task-related untracked
  file before editing. Report only actionable findings with severity (`critical`, `high`,
  `medium`, `low`) and file/line evidence."

If specialized subagents are unavailable, perform the same stages sequentially, label each pass,
and keep Explorer read-only and Reviewer independent from implementation reasoning.

## Integrity rules

- Do not expose or modify secrets or `.env` files.
- Do not install dependencies, deploy, publish, push, merge, or commit without user authorization.
- Do not weaken tests, validation, or security controls to obtain a green result.
- Distinguish `passed`, `failed`, `not run`, and `not configured`; never infer success.
- Use only the verification commands documented in `AGENTS.md` and the repository.

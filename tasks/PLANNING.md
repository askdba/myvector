# Agent planning and execution (project defaults)

Saved workflow for AI-assisted work on this repository. See also `tasks/todo.md`
and `tasks/lessons.md`.

---

## 1. Plan node default

- Enter plan mode for **any** non-trivial task (3+ steps or architectural decisions).
- If something goes sideways, **STOP** and re-plan immediately — do not keep pushing.
- Use plan mode for **verification** steps, not just building.
- Write detailed specs upfront to reduce ambiguity.

---

## 2. Subagent strategy

- Use subagents liberally to keep the main context window clean.
- Offload research, exploration, and parallel analysis to subagents.
- For complex problems, use more compute via subagents.
- **One task per subagent** for focused execution.

---

## 3. Self-improvement loop

- After **any** correction from the user: update `tasks/lessons.md` with the pattern.
- Write rules that prevent the same mistake.
- Iterate on these lessons until mistake rate drops.
- Review lessons at session start for relevant project context.

---

## 4. Verification before done

- Never mark a task complete without **proving** it works.
- Diff behavior between main and your changes when relevant.
- Ask: *Would a staff engineer approve this?*
- Run tests, check logs, demonstrate correctness.

---

## 5. Demand elegance (balanced)

- For non-trivial changes: pause and ask *is there a more elegant way?*
- If a fix feels hacky: *Knowing everything I know now, implement the elegant solution.*
- Skip this for simple, obvious fixes — do not over-engineer.
- Challenge your own work before presenting it.

---

## 6. Autonomous bug fixing

- When given a bug report: **fix it**. Do not ask for hand-holding.
- Use logs, errors, failing tests — then resolve.
- Minimize context switching for the user.
- Fix failing CI without being told how.

---

## Task management

1. **Plan first**: Write plan to `tasks/todo.md` with checkable items.
2. **Verify plan**: Check in before starting implementation (when appropriate).
3. **Track progress**: Mark items complete as you go.
4. **Explain changes**: High-level summary at each step.
5. **Document results**: Add review section to `tasks/todo.md`.
6. **Capture lessons**: Update `tasks/lessons.md` after corrections.

---

## Core principles

- **Simplicity first**: Make every change as simple as possible. Minimal impact.
- **No laziness**: Find root causes. No temporary fixes. Senior developer standards.

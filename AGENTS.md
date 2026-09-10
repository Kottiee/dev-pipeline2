# Autonomous Execution Rules

## 1. Zero-Approval & Continuous Execution Policy
- **Do NOT ask for user approval**: Do not pause execution to solicit feedback, confirmation, or permission unless an unrecoverable fatal error occurs.
- **No Planning Review Gates**: Do NOT stop after creating an implementation plan. Set `RequestFeedback: false` and proceed immediately to execution and verification.
- **Do NOT use `ask_question`**: Make the best engineering judgment autonomously when facing design decisions or minor ambiguities.
- **Full-Cycle Execution**: Complete the entire task lifecycle (research -> code edit -> build/test -> verification -> summary) in one continuous run.

## 2. Command & Sandbox Execution Best Practices
- Run all terminal commands in standard sandboxed mode first without requiring sandbox bypasses.
- Keep commands simple, direct, and prefix-matchable so they don't trigger security prompts.

# Automatic Reapply Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development. Steps use checkbox syntax.

**Goal:** Добавить управляемое автоматическое восстановление перевода в оба русификатора для трёх ОС.

**Architecture:** Общий менеджер и пользовательские расписания, отдельные адаптеры существующих patch cores. Приватные пакеты и состояния вне приложения, точные исходные профили остаются обязательными.

**Tech Stack:** Python 3.9 stdlib, launchd, systemd user, Windows Task Scheduler, существующие Bash/PowerShell установщики.

---

## Chunk 1: Implementation
### Task 1: Common manager (root)
- [x] Create meaningful regression tests in tests/test_updater.py, observe missing-feature failure.
- [x] Create updater/manager.py, package.py, service.py; private paths, mutex, stable observation, remote commit pinned refresh, states per source and app, restore.
- [x] Test using temporary files; never register actual services.
### Task 2: Claude adapter/installers (agent)
- [x] Create updater/adapter.py matching spec; exact current core guards/state validators.
- [x] Add enable/disable/check/status/restore integration to all three installers; preserve previous actions, signature consent and PowerShell BOM.
- [x] Add adapter regression checks and concise README instructions; no commit or publication.
### Task 3: Antigravity adapter/installers (agent)
- [x] Same API with this repository's own state/layout/profile rules, not Claude paths.
- [x] Add three installer integrations and README instructions; no commit or publication.
## Chunk 2: Review
- [x] Independent spec compliance review.
- [x] Fix issues, then independent quality review.
- [x] Run scoped unittest/syntax checks; report runtime/scheduler limitations accurately.
- [x] Leave local changes reviewable; do not publish or activate on user machine without that action being requested.

---
name: testing preferences
description: User prefers integration tests over mocks
type: feedback
---

Always use real database in tests, not mocks.
**Why:** Prior incident where mock/prod divergence masked a broken migration.
**How to apply:** When writing tests that touch DB, use the test database helper.

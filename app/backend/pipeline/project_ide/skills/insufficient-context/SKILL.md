---
name: insufficient-context
description: Handle missing decisive implementation, dependency versions, evidence, or verification without inventing results.
---

Use read and search to gather available context. If the decisive fact remains unavailable, record status insufficient_context, findings [], validation_state not_validated, and a specific nonempty missing_context list. Ask a precise user question when their input can supply that fact. A symbol name, search snippet, unresolved version range, or failed lookup cannot establish the missing implementation or safety result. Do not fabricate code placeholders as if they were retrieved source.

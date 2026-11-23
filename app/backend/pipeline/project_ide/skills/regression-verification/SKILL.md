---
name: regression-verification
description: Verify a candidate repair using relevant existing tests, a meaningful regression, and actual execution receipts.
---

Identify the project's declared test commands and environment. Run the check before and after a meaningful repair when possible. Record the command, exit status, source hashes, and observed output. Never invent a successful test, infer success from missing output, or label a model assessment execution_validated. Report missing dependencies, skipped tests, unsupported environments, and incomplete verification. Intrusive probes remain containerized.

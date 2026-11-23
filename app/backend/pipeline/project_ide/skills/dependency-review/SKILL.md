---
name: dependency-review
description: Review selected exact dependency releases with installed metadata, public advisories, source provenance, and explicit coverage.
---

Keep declared ranges, locked versions, and installed versions distinct. Review only packages explicitly selected for source review. Use actual installed or exact published release sources, read-only. Record origin and digest. Never run installation scripts or patch installed dependency folders. Keep outdated status separate from vulnerability status. Missing or failed lookups are inconclusive. Say "No known vulnerabilities found in the checked sources" only when those lookups succeeded. Propose remediation as a separate task.

# Create a Capability

This guide walks engineers through registering a new platform capability.

## Overview

A **capability** is a durable product surface with a clear owner, SLA, and dependency graph entry in the city map. All new capabilities must be registered before production traffic.

## Prerequisites

- Access to the platform Jira project (`CAP`)
- Write access to the city-map repository
- Membership in `#capability-help` and `#platform-oncall`

## Checklist

1. **Open a CAP ticket** — Use the "New Capability" template. Include problem statement, target consumers, and proposed SLA.
2. **Author a capability manifest** — Create `manifest.yaml` with `name`, `owner_team`, `sla`, and `dependencies`.
3. **Update the city map** — Open a PR adding a node for your capability and edges to upstream dependencies.
4. **Platform review** — Request review from the platform team. Typical turnaround is 2–3 business days.
5. **Publish DevPortal page** — Document how consumers integrate with your capability.

## Manifest example

```yaml
name: payments-v2
owner_team: money-movement
sla: 99.9
dependencies:
  - auth-gateway
  - ledger-core
```

## Common failures

- **Missing owner team** — The registry syncs ownership from the city map, not the manifest alone.
- **Unresolved dependencies** — Every dependency must already exist as a city-map node.
- **Skipped CAP ticket** — Platform review will reject PRs without a linked CAP issue.

## Related

- City Map contribution guide
- CAP-88 (docs checklist gaps — resolved)
- Slack `#capability-help` for day-to-day questions

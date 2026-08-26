# Capability Lifecycle

Capabilities move through stages: **Proposed → Registered → Active → Deprecated**.

## Proposed

A CAP ticket exists and a draft city-map node is under review. No production traffic.

## Registered

City-map PR merged, manifest validated, owner team confirmed. Consumers may begin integration in non-prod.

## Active

Production traffic allowed. SLA monitoring enabled. Breaking changes require a versioned migration plan.

## Deprecated

Owner announces sunset timeline (minimum 90 days). Dependents must migrate. City-map node is marked `deprecated`.

## Ownership changes

Ownership transfers require:

1. Updated `owner_team` on the city-map node
2. Linked CAP ticket for the transfer
3. Handoff note posted in `#capability-help`

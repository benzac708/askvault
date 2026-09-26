# Incident Response Runbook

## Declaring an incident

An incident is declared in the #incidents channel by anyone who notices customer
impact. Declaring early is always cheaper than declaring late. Post the impact
summary within ten minutes even if the cause is still unknown.

## Severity levels

SEV1 is total unavailability or data loss. SEV2 is degraded service affecting more
than ten percent of requests. Everything else is SEV3 and is handled in business
hours.

## Escalation

If the primary on-call does not acknowledge a SEV1 within five minutes, the
secondary is paged automatically. If neither responds within ten minutes, the
incident commander is woken and the VP Engineering is notified by SMS.

## After the incident

A written postmortem is required for every SEV1 and SEV2 within five business
days. Postmortems are blameless and are read by the whole engineering org.

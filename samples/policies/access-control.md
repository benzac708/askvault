# Access Control Policy

## Principle of least privilege

Every grant is scoped to the smallest set of permissions that lets a person do
their job. Standing production access is prohibited; all production access is
time-boxed and logged.

## Secrets

Secrets never enter a repository, a ticket, or a chat message. They are issued by
the secrets manager and referenced by name. Leaked credentials are rotated
without discussion, and rotation is not treated as an incident unless exposure
was external.

## Access reviews

Managers review their team's grants every quarter. Anything untouched for two
consecutive quarters is revoked automatically, without exception and without
asking the holder first.

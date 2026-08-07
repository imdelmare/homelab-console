# Public capability decision

## Scope

Failover to a preconfigured backup gateway and restore of the primary gateway.

Exact tool identifiers:

- `opnsense.gateway.failover`
- `opnsense.gateway.restore`

## Safety contract

- Targets are closed enums or validated inventory identifiers.
- Every write requires a fresh, input-bound, single-use operator approval.
- Execution passes through the shared execution core, redaction, and audit.
- Provider credentials must be least-privilege and separate from read access.
- Implementations perform a typed post-action read-back where supported.

## Deployment status

This sanitized record documents the reference implementation only. It is not
authorization to activate the capability in another deployment. Operators must
review provider ACLs, rollback, recovery access, and focused tests before
enabling any write tool.

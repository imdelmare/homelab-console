# Public capability decision

## Scope

Wake-on-LAN for targets declared in inventory and the firewall plugin.

Exact tool identifiers:

- `opnsense.wol.wake`

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

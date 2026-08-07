"""Machine-readable capability approvals governed by operator-reviewed ADRs."""

# Map an exact write tool id to the repository-relative ADR that authorizes
# live activation. Design and test implementations do not belong here.
APPROVED_WRITE_TOOLS: dict[str, str] = {
    # Operator-approved on 2026-07-28 (live drill required by the milestone).
    "adguard.protection.pause": "docs/decisions/0004-first-live-write-capabilities.md",
    "adguard.protection.resume": "docs/decisions/0004-first-live-write-capabilities.md",
    # Operator-approved on 2026-07-29 for a least-privilege drill on LXC 121.
    "proxmox.lxc.start": "docs/decisions/0006-activate-proxmox-lxc-drill.md",
    "proxmox.lxc.shutdown": "docs/decisions/0006-activate-proxmox-lxc-drill.md",
    # Operator-approved on 2026-07-30 for the scoped operator-workstation WoL drill.
    "opnsense.wol.wake": "docs/decisions/0008-activate-opnsense-wol-drill.md",
    # Operator-approved on 2026-07-30 for failover then immediate restore.
    "opnsense.gateway.failover": "docs/decisions/0010-activate-opnsense-gateway-drill.md",
    "opnsense.gateway.restore": "docs/decisions/0010-activate-opnsense-gateway-drill.md",
}

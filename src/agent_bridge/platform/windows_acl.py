"""Pure-text verification for the ACL emitted by Windows ``icacls``."""

from __future__ import annotations


WINDOWS_OWNER_ONLY_GUARANTEE = (
    "No principal other than the owner, SYSTEM, and Administrators has any access."
)

_OWNER_ALIASES = {"OWNER RIGHTS", "S-1-3-4"}
_SYSTEM_ALIASES = {"NT AUTHORITY\\SYSTEM", "SYSTEM", "S-1-5-18"}
_ADMINISTRATOR_ALIASES = {
    "BUILTIN\\ADMINISTRATORS", "ADMINISTRATORS", "S-1-5-32-544",
}


def icacls_listing_is_owner_only(text: str, owner_sid: str,
                                 owner_name: str = "") -> bool:
    """Prove that only the owner, SYSTEM, and Administrators have access."""
    if not owner_sid.startswith("S-1-"):
        return False
    allowed = ({owner_sid.upper()} | _OWNER_ALIASES | _SYSTEM_ALIASES
               | _ADMINISTRATOR_ALIASES)
    if owner_name:
        allowed.add(owner_name.upper())
    principals: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        marker = stripped.rfind(":(")
        if marker < 0:
            continue
        principal = stripped[:marker]
        if not principals:
            matches = [candidate for candidate in allowed
                       if principal.upper().endswith(candidate)
                       and len(principal) > len(candidate)
                       and principal[-len(candidate) - 1].isspace()]
            if not matches:
                return False
            principal = max(matches, key=len)
        principal = principal.strip().upper()
        if not principal or principal not in allowed or "(I)" in stripped:
            return False
        principals.append(principal)
    return bool(principals) and bool(set(principals) & ({owner_sid.upper()}
                                                        | _OWNER_ALIASES))

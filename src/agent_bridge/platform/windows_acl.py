"""Pure-text verification for the ACL emitted by Windows ``icacls``."""

from __future__ import annotations


WINDOWS_OWNER_ONLY_GUARANTEE = (
    "No principal other than the owner, SYSTEM, and Administrators has any access."
)

# KNOWN LIMITATION: these are the English icacls names. On a localized Windows
# install icacls prints, for example, VORDEFINIERT\\Administratoren, which will
# not match and the check will fail closed. That is safe, and it means the tool
# refuses to start there rather than running with an unverified guarantee.
#
# The locale-proof fix is to read SDDL instead of display names, where the same
# principals appear as fixed abbreviations (BA, SY, OW). Not done here because
# nobody on this side can test Windows at all, let alone a localized one.
# Worth doing when someone on a non-English install actually needs it.
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
    # The owner may appear as the SID, as OWNER RIGHTS, or as the resolved
    # account name. icacls resolves a SID to a name for display, so granting by
    # *SID and then reading back commonly yields the NAME, which is exactly what
    # a file with a single owner ACE looks like. Omitting the name here rejected
    # the strictest possible ACL: one ACE, the owner, nobody else.
    owner_forms = {owner_sid.upper()} | _OWNER_ALIASES
    if owner_name:
        owner_forms.add(owner_name.upper())
    return bool(principals) and bool(set(principals) & owner_forms)

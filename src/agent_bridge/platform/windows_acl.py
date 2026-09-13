"""Pure-text verification for the ACL emitted by Windows ``icacls``."""

from __future__ import annotations

import re


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


_SUMMARY = re.compile(
    r"^Successfully processed \d+ files; Failed processing \d+ files$",
    re.IGNORECASE,
)
_ACE = re.compile(r"^(?P<principal>[^:\r\n]+):(?P<rights>(?:\([^)\r\n]+\))+)$")


def icacls_listing_is_owner_only(text: str, owner_sid: str,
                                 owner_name: str = "",
                                 expected_path: str | None = None) -> bool:
    """Prove that only the owner, SYSTEM, and Administrators have access."""
    if not owner_sid.startswith("S-1-"):
        return False
    allowed = ({owner_sid.upper()} | _OWNER_ALIASES | _SYSTEM_ALIASES
               | _ADMINISTRATOR_ALIASES)
    if owner_name:
        allowed.add(owner_name.upper())
    principals: list[str] = []
    first_ace = True
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _SUMMARY.fullmatch(stripped):
            continue
        ace_text = stripped
        if first_ace and expected_path is not None:
            prefix = line[:len(expected_path)]
            remainder = line[len(expected_path):]
            # icacls prints the target only on the first ACE line.  Match its
            # whole prefix (case-insensitively, as Windows paths are) and the
            # following separator; suffix matching a permitted principal lets
            # an attacker make an untrusted account name look like SYSTEM.
            if (prefix.casefold() != expected_path.casefold()
                    or not remainder or not remainder[0].isspace()):
                return False
            ace_text = remainder.strip()
        ace = _ACE.fullmatch(ace_text)
        if ace is None:
            # Every nonblank line must be either a known icacls summary or a
            # complete ACE.  Ignoring malformed ACE-looking output turns an
            # unverified ACL into an apparent success.
            return False
        principal = ace.group("principal").strip().upper()
        rights = ace.group("rights").upper()
        if not principal or principal not in allowed or "(I)" in rights:
            return False
        principals.append(principal)
        first_ace = False
    # The owner may appear as the SID, as OWNER RIGHTS, or as the resolved
    # account name. icacls resolves a SID to a name for display, so granting by
    # *SID and then reading back commonly yields the NAME, which is exactly what
    # a file with a single owner ACE looks like. Omitting the name here rejected
    # the strictest possible ACL: one ACE, the owner, nobody else.
    owner_forms = {owner_sid.upper()} | _OWNER_ALIASES
    if owner_name:
        owner_forms.add(owner_name.upper())
    return bool(principals) and bool(set(principals) & owner_forms)

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


def _allowed_principals(owner_sid: str, owner_name: str = "") -> set[str]:
    allowed = ({owner_sid.upper()} | _OWNER_ALIASES | _SYSTEM_ALIASES
               | _ADMINISTRATOR_ALIASES)
    if owner_name:
        allowed.add(owner_name.upper())
    return allowed


def icacls_listing_aces(text: str,
                        expected_path: str | None = None
                        ) -> list[tuple[str, str]] | None:
    """Every ACE in one object's icacls listing, or None if it is malformed.

    Each entry is ``(principal, rights)`` as printed, upper-cased. The first
    nonblank line must begin with ``expected_path`` followed by whitespace:
    icacls prints the target only there, and a suffix match would let an
    untrusted account name end in ``SYSTEM``. Every other nonblank line must
    be a complete ACE or the icacls summary. Anything else, including an
    object that prints no ACE at all, is None, and None is never "verified".
    """
    aces: list[tuple[str, str]] = []
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
            if (prefix.casefold() != expected_path.casefold()
                    or not remainder or not remainder[0].isspace()):
                return None
            ace_text = remainder.strip()
        ace = _ACE.fullmatch(ace_text)
        if ace is None:
            # Every nonblank line must be either a known icacls summary or a
            # complete ACE.  Ignoring malformed ACE-looking output turns an
            # unverified ACL into an apparent success.
            return None
        principal = ace.group("principal").strip().upper()
        if not principal:
            return None
        aces.append((principal, ace.group("rights").upper()))
        first_ace = False
    return aces


def icacls_listing_is_owner_only(text: str, owner_sid: str,
                                 owner_name: str = "",
                                 expected_path: str | None = None,
                                 *, allow_inherited: bool = False) -> bool:
    """Prove that only the owner, SYSTEM, and Administrators have access.

    ``allow_inherited`` is for an object inside a tree whose root has already
    been proved owner-only with a protected DACL: an inherited entry there is
    a copy of an entry on an ancestor that is itself in the listing under
    proof, so the principal check on every object covers it. The object a
    caller protects directly is proved with inherited entries refused, since
    those would come from a parent this call knows nothing about.
    """
    if not owner_sid.startswith("S-1-"):
        return False
    aces = icacls_listing_aces(text, expected_path)
    if not aces:
        return False
    allowed = _allowed_principals(owner_sid, owner_name)
    principals: list[str] = []
    for principal, rights in aces:
        if principal not in allowed:
            return False
        if "(I)" in rights and not allow_inherited:
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
    return bool(set(principals) & owner_forms)


def icacls_listing_foreign_principals(text: str, owner_sid: str,
                                      owner_name: str = "",
                                      expected_path: str | None = None
                                      ) -> list[str] | None:
    """The explicit principals an owner-only ACL must not contain, as printed.

    These are the names to hand to ``icacls /remove``. Inherited entries are
    not listed: they cannot be removed one by one, and ``/inheritance:r`` or
    propagation from a protected parent is what clears them. None means the
    listing could not be read at all and nothing should be inferred from it.
    """
    aces = icacls_listing_aces(text, expected_path)
    if aces is None:
        return None
    allowed = _allowed_principals(owner_sid, owner_name)
    seen: list[str] = []
    for principal, rights in aces:
        if principal in allowed or "(I)" in rights or principal in seen:
            continue
        seen.append(principal)
    return seen


def split_icacls_tree_listing(text: str) -> list[str]:
    """One block of text per object in an ``icacls <dir> /t`` listing.

    icacls prints each object's path at the start of a line, its first ACE
    on that same line and the rest indented beneath it; an object with an
    empty ACL prints its path and nothing else. Blank lines are not a
    reliable separator (icacls omits them after an empty object), so a block
    starts at every line that begins in column one and is not the summary.
    """
    blocks: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or _SUMMARY.fullmatch(stripped):
            continue
        if line[0].isspace():
            if blocks:
                blocks[-1].append(line)
            else:
                # An ACE before any object: not a listing this parser knows.
                return []
        else:
            blocks.append([line])
    return ["\n".join(block) for block in blocks]


def match_icacls_tree_blocks(text: str, root: str, expected_paths: list[str]
                             ) -> list[tuple[str, str]] | None:
    """Pair each block of a recursive listing with the object it describes.

    icacls prints the path and the first ACE on one line, and a path may
    contain spaces, so the object is recognised from the caller's own list
    of expected paths: the longest one the line starts with, followed by
    whitespace or the end of the line. None if any block names an object
    the caller did not expect, or one twice; the caller then knows nothing
    about that block and must not infer anything from the listing.
    """
    wanted = {path.casefold(): path for path in expected_paths}
    wanted.setdefault(root.casefold(), root)
    by_length = sorted(wanted, key=len, reverse=True)
    matched: list[tuple[str, str]] = []
    seen: set[str] = set()
    for block in split_icacls_tree_listing(text):
        first = block.splitlines()[0]
        folded = first.casefold()
        match = next((key for key in by_length
                      if folded.startswith(key)
                      and (len(first) == len(key) or first[len(key)].isspace())),
                     None)
        if match is None or match in seen:
            return None
        seen.add(match)
        matched.append((wanted[match], block))
    return matched


def icacls_tree_foreign_principals(text: str, owner_sid: str, owner_name: str,
                                   root: str, expected_paths: list[str]
                                   ) -> list[str] | None:
    """Every explicit foreign principal anywhere in a recursive listing.

    The names to hand to ``icacls <root> /t /remove``. None if the listing
    names an object the caller did not expect, since a listing that does
    not match the tree the caller walked is not one to act on.
    """
    matched = match_icacls_tree_blocks(text, root, expected_paths)
    if matched is None:
        return None
    foreign: list[str] = []
    for path, block in matched:
        names = icacls_listing_foreign_principals(
            block, owner_sid, owner_name, expected_path=path)
        # A block with no readable ACE (an empty ACL) has nothing to remove;
        # the read-back is what judges it.
        for name in names or []:
            if name not in foreign:
                foreign.append(name)
    return foreign


def icacls_tree_listing_is_owner_only(text: str, owner_sid: str,
                                      owner_name: str, root: str,
                                      expected_paths: list[str]
                                      ) -> tuple[bool, list[str]]:
    """Prove every object of a store owner-only from one recursive listing.

    The root is proved strictly (explicit entries only, owner present); it
    has ``/inheritance:r``, so nothing flows into the tree from outside it.
    Every descendant must have only permitted principals and the owner among
    them, inherited entries allowed: an inherited entry on a descendant is a
    copy of an entry on an ancestor that this same listing proves, or a
    stale copy from a former parent, which the principal check refuses as
    surely as an explicit one. The listing must name exactly the expected
    objects, each once: an object that appeared after the caller walked the
    store, or one the caller saw that icacls did not, fails the proof.

    Returns the verdict and the paths that failed, for evidence.
    """
    if not owner_sid.startswith("S-1-"):
        return False, [root]
    matched = match_icacls_tree_blocks(text, root, expected_paths)
    if matched is None:
        return False, [root]
    failed: list[str] = []
    for path, block in matched:
        strict = path.casefold() == root.casefold()
        if not icacls_listing_is_owner_only(
                block, owner_sid, owner_name, expected_path=path,
                allow_inherited=not strict):
            failed.append(path)
    seen = {path.casefold() for path, _block in matched}
    missing = [path for path in [root, *expected_paths]
               if path.casefold() not in seen]
    if missing or failed:
        return False, failed + missing
    return True, []

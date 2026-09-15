"""Pure, host-independent half of the Windows owner-only ACL guarantee.

Everything here is bytes and policy, with no Windows call in it, so it runs
and is tested on every platform. The Windows platform layer reads a
security descriptor through a handle, hands the raw owner SID and DACL
bytes to :func:`parse_sid` and :func:`parse_acl`, and asks
:func:`judge_security` whether the result is owner-only. To protect an
object it writes the exact DACL :func:`build_owner_only_acl` returns and
then checks the read-back with :func:`is_exactly_owner_only`.

Why bytes rather than ``icacls`` text: the text is localized (a German
install prints ``VORDEFINIERT\\Administratoren``), it names principals by
display name, it never shows who owns the object, and every step through
it is a process spawn addressing the object by path. The binary security
descriptor is none of those things.

Ownership matters as much as the entries. An ``OWNER RIGHTS`` entry
(S-1-3-4) grants access to whoever owns the object, so a protected file
owned by another account and carrying ``OWNER RIGHTS:(F)`` is readable by
that account while looking owner-only. The judgment therefore requires
the object's owner to be the calling account before any entry is
credited, and refuses to count an entry for anyone else at all.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct


WINDOWS_OWNER_ONLY_GUARANTEE = (
    "No principal other than the owner, SYSTEM, and Administrators has any access."
)

# Well-known SIDs, by value rather than by any display name.
SID_OWNER_RIGHTS = "S-1-3-4"
SID_SYSTEM = "S-1-5-18"
SID_ADMINISTRATORS = "S-1-5-32-544"

# ACE header types that can appear in a DACL. Anything else (object ACEs,
# callback or conditional ACEs, audit types that belong in a SACL) is
# refused rather than interpreted.
ACCESS_ALLOWED_ACE_TYPE = 0x00
ACCESS_DENIED_ACE_TYPE = 0x01

# ACE header flags.
OBJECT_INHERIT_ACE = 0x01
CONTAINER_INHERIT_ACE = 0x02
NO_PROPAGATE_INHERIT_ACE = 0x04
INHERIT_ONLY_ACE = 0x08
INHERITED_ACE = 0x10

# Security descriptor control bits the judgment reads.
SE_DACL_PRESENT = 0x0004
SE_DACL_PROTECTED = 0x1000

FILE_ALL_ACCESS = 0x001F01FF

ACL_REVISION = 2
SID_REVISION = 1
SID_MAX_SUB_AUTHORITIES = 15

_ACL_HEADER = struct.Struct("<BBHHH")      # revision, sbz1, size, count, sbz2
_ACE_HEADER = struct.Struct("<BBH")        # type, flags, size
_ACE_MASK = struct.Struct("<I")


@dataclass(frozen=True)
class Ace:
    """One access control entry as read back, principal by SID string.

    ``sid`` is None for an ACE type this module does not interpret; such
    an entry never passes judgment.
    """

    type: int
    flags: int
    mask: int
    sid: str | None


@dataclass(frozen=True)
class SecurityState:
    """What one object's security descriptor says, once decoded.

    ``dacl`` is None when the descriptor has no DACL or a NULL one, which
    Windows treats as "everyone has every access". An empty tuple is an
    empty DACL, which grants nobody anything, and is not owner-only either:
    the owner cannot open it, so it is not the state anything here writes.
    """

    owner_sid: str
    protected: bool
    dacl: tuple[Ace, ...] | None
    is_directory: bool
    sddl: str = ""


# ---------------------------------------------------------------------------
# SID codec
# ---------------------------------------------------------------------------


def parse_sid(data: bytes) -> str:
    """Decode a binary SID into its canonical ``S-1-...`` string. Strict."""
    if len(data) < 8:
        raise ValueError("SID shorter than its header")
    revision, count = data[0], data[1]
    if revision != SID_REVISION:
        raise ValueError(f"unsupported SID revision {revision}")
    if count > SID_MAX_SUB_AUTHORITIES:
        raise ValueError(f"SID claims {count} sub-authorities")
    if len(data) != 8 + 4 * count:
        raise ValueError("SID length does not match its sub-authority count")
    authority = int.from_bytes(data[2:8], "big")
    if authority >= 1 << 32:
        parts = [f"S-{revision}-0x{authority:012X}"]
    else:
        parts = [f"S-{revision}-{authority}"]
    for index in range(count):
        parts.append(str(struct.unpack_from("<I", data, 8 + 4 * index)[0]))
    return "-".join(parts)


def encode_sid(text: str) -> bytes:
    """The inverse of :func:`parse_sid`. Refuses anything not shaped like a SID."""
    fields = text.strip().split("-")
    if len(fields) < 3 or fields[0].upper() != "S":
        raise ValueError(f"not a SID: {text!r}")
    try:
        revision = int(fields[1])
        authority_text = fields[2]
        authority = (int(authority_text, 16) if authority_text.lower().startswith("0x")
                     else int(authority_text))
        subs = [int(part) for part in fields[3:]]
    except ValueError:
        raise ValueError(f"not a SID: {text!r}") from None
    if revision != SID_REVISION or not 0 <= authority < 1 << 48:
        raise ValueError(f"not a SID: {text!r}")
    if len(subs) > SID_MAX_SUB_AUTHORITIES or any(not 0 <= s < 1 << 32 for s in subs):
        raise ValueError(f"not a SID: {text!r}")
    out = bytes([revision, len(subs)]) + authority.to_bytes(6, "big")
    for sub in subs:
        out += struct.pack("<I", sub)
    return out


def canonical_sid(text: str) -> str:
    """``text`` as :func:`parse_sid` would print it, or ValueError."""
    return parse_sid(encode_sid(text))


# ---------------------------------------------------------------------------
# ACL codec
# ---------------------------------------------------------------------------


def acl_size(header: bytes) -> int:
    """The byte length an ACL header claims for the whole ACL."""
    if len(header) < _ACL_HEADER.size:
        raise ValueError("ACL header truncated")
    _revision, _sbz1, size, _count, _sbz2 = _ACL_HEADER.unpack_from(header)
    if size < _ACL_HEADER.size:
        raise ValueError("ACL claims a size smaller than its header")
    return size


def parse_acl(data: bytes) -> tuple[Ace, ...]:
    """Decode a binary ACL. Strict: any inconsistency raises ValueError.

    Only ACCESS_ALLOWED and ACCESS_DENIED entries are decoded to a SID.
    Every other type is kept with ``sid=None`` so the judgment can refuse
    it; it is not skipped, because an entry this code does not understand
    is not one it can vouch for.
    """
    if len(data) < _ACL_HEADER.size:
        raise ValueError("ACL shorter than its header")
    revision, _sbz1, size, count, _sbz2 = _ACL_HEADER.unpack_from(data)
    if revision not in (2, 4):
        raise ValueError(f"unsupported ACL revision {revision}")
    if size != len(data):
        raise ValueError("ACL size does not match the bytes supplied")
    aces: list[Ace] = []
    offset = _ACL_HEADER.size
    for _ in range(count):
        if offset + _ACE_HEADER.size > size:
            raise ValueError("ACE header runs past the ACL")
        ace_type, ace_flags, ace_size = _ACE_HEADER.unpack_from(data, offset)
        if ace_size < _ACE_HEADER.size or offset + ace_size > size:
            raise ValueError("ACE size runs past the ACL")
        body = data[offset + _ACE_HEADER.size: offset + ace_size]
        if ace_type in (ACCESS_ALLOWED_ACE_TYPE, ACCESS_DENIED_ACE_TYPE):
            if len(body) < _ACE_MASK.size + 8:
                raise ValueError("access ACE too short for a mask and a SID")
            mask = _ACE_MASK.unpack_from(body)[0]
            sid_bytes = body[_ACE_MASK.size:]
            # An ACE may be padded to a 4-byte boundary; the SID says how
            # long it really is.
            claimed = 8 + 4 * sid_bytes[1]
            if claimed > len(sid_bytes):
                raise ValueError("ACE SID runs past the ACE")
            aces.append(Ace(ace_type, ace_flags, mask, parse_sid(sid_bytes[:claimed])))
        else:
            aces.append(Ace(ace_type, ace_flags, 0, None))
        offset += ace_size
    if offset != size:
        raise ValueError("ACL holds bytes after its last ACE")
    return tuple(aces)


def owner_only_aces(owner_sid: str, *, directory: bool) -> tuple[Ace, ...]:
    """The one entry an owner-only object carries, exactly."""
    flags = (OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE) if directory else 0
    return (Ace(ACCESS_ALLOWED_ACE_TYPE, flags, FILE_ALL_ACCESS, canonical_sid(owner_sid)),)


def build_owner_only_acl(owner_sid: str, *, directory: bool) -> bytes:
    """A complete binary DACL granting the owner everything and nobody else.

    Directories get (OI)(CI) so what is created inside inherits the same
    single entry; files get a plain (F). The result is what SetSecurityInfo
    writes in one call, so there is never an intermediate DACL between the
    one found and this one.
    """
    sid = encode_sid(owner_sid)
    (ace,) = owner_only_aces(owner_sid, directory=directory)
    ace_bytes = _ACE_HEADER.pack(ace.type, ace.flags, _ACE_HEADER.size + 4 + len(sid))
    ace_bytes += _ACE_MASK.pack(ace.mask) + sid
    return _ACL_HEADER.pack(ACL_REVISION, 0, _ACL_HEADER.size + len(ace_bytes), 1, 0) + ace_bytes


# ---------------------------------------------------------------------------
# Judgment
# ---------------------------------------------------------------------------


def judge_security(state: SecurityState, caller_sid: str, *,
                   allow_inherited: bool = False) -> list[str]:
    """Every reason ``state`` is not owner-only for ``caller_sid``. Empty is a pass.

    The rules, in the order they are checked:

    * The object must be owned by the caller. Not "an administrator", not
      "somebody with OWNER RIGHTS": an object another account owns inside
      the caller's store is that account's to read, whatever the DACL
      says, and is refused as such.
    * There must be a DACL and it must not be NULL (which is "everyone").
      An empty DACL is refused too: nobody can open it, and it is never
      the state this module writes.
    * Unless ``allow_inherited``, the DACL must be protected and carry no
      inherited entry. An object proved on its own stands on its own
      entries. ``allow_inherited`` is for a descendant of a protected root
      the caller has proved separately, whose inherited entries can only
      be copies of that root's.
    * Every entry must be ACCESS_ALLOWED for the caller, SYSTEM,
      Administrators or OWNER RIGHTS (credited to the caller only because
      the first rule already made the caller the owner). A deny entry, an
      entry of a type this module does not decode, or any other principal,
      including an inherit-only entry that grants nothing on this object
      but would on its children, is refused.
    * The caller must actually appear, directly or as OWNER RIGHTS.
    """
    try:
        caller = canonical_sid(caller_sid)
    except ValueError:
        return ["caller identity unknown"]
    problems: list[str] = []
    if state.owner_sid != caller:
        problems.append(f"owned by {state.owner_sid or 'nobody'}, not the caller")
    if state.dacl is None:
        problems.append("no DACL (everyone has every access)")
        return problems
    if not state.dacl:
        problems.append("empty DACL")
    if not allow_inherited and not state.protected:
        problems.append("DACL inherits from the parent")
    owner_present = False
    for ace in state.dacl:
        if ace.sid is None:
            problems.append(f"ACE type 0x{ace.type:02X} is not interpreted")
            continue
        sid = ace.sid
        if ace.type != ACCESS_ALLOWED_ACE_TYPE:
            problems.append(f"{sid} carries a non-allow entry (type 0x{ace.type:02X})")
            continue
        if ace.flags & INHERITED_ACE and not allow_inherited:
            problems.append(f"{sid} entry is inherited")
        if sid in (caller, SID_OWNER_RIGHTS):
            owner_present = True
        elif sid in (SID_SYSTEM, SID_ADMINISTRATORS):
            continue
        else:
            problems.append(f"{sid} has access")
    if state.dacl and not owner_present:
        problems.append("the caller has no entry")
    return problems


def is_exactly_owner_only(state: SecurityState, caller_sid: str) -> bool:
    """Whether ``state`` is exactly the DACL :func:`build_owner_only_acl` writes.

    The read-back after protecting an object is held to this, stricter
    than :func:`judge_security`: what was written is what must be there,
    and nothing else, on an object the caller owns.
    """
    if judge_security(state, caller_sid) != []:
        return False
    return bool(state.protected and state.dacl is not None
                and tuple(state.dacl) == owner_only_aces(
                    caller_sid, directory=state.is_directory))

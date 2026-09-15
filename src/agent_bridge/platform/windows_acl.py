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

"The calling account" has two SIDs, not one. Windows makes the creator
of a new object its owner, except that an elevated member of
Administrators gets the Administrators group as the default owner of
everything it creates (the hosted Windows runner, run 34940818792, showed
every temporary file owned by S-1-5-32-544). The token reports that
default owner directly (TokenOwner), so the judgment accepts an object
owned by either the token's user or the token's default owner and
refuses every other owner. A non-elevated account's default owner is
itself, so for it nothing changes: an Administrators-owned file is still
another account's.

Beyond the descriptor itself, this module also decodes two other byte
formats the platform layer reads through handles: the self-relative
security descriptor ``NtQuerySecurityObject`` returns, and the
``FILE_FULL_DIR_INFO`` records a directory handle enumerates. Both are
decoded here so that the decoding is tested on every platform.
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

# Security descriptor control bits the judgment reads and the writer sets.
SE_DACL_PRESENT = 0x0004
SE_DACL_PROTECTED = 0x1000
SE_SELF_RELATIVE = 0x8000
SECURITY_DESCRIPTOR_REVISION = 1

# Access masks. FILE_ALL_ACCESS is what the writer grants. The generic
# read and write masks are the least an entry must grant, once
# inherit-only entries are set aside, for the caller to actually be able
# to read and write the object: an entry with a zero mask, or one that
# applies only to future children, names the caller without letting the
# caller in, and a store whose owner cannot read it is not ready.
FILE_ALL_ACCESS = 0x001F01FF
FILE_GENERIC_READ = 0x00120089
FILE_GENERIC_WRITE = 0x00120116
FILE_GENERIC_EXECUTE = 0x001200A0
REQUIRED_OWNER_ACCESS = FILE_GENERIC_READ | FILE_GENERIC_WRITE
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
GENERIC_EXECUTE = 0x20000000
GENERIC_ALL = 0x10000000
#: What each generic right means once mapped onto a file object, the way
#: the kernel maps it before an access check (GENERIC_MAPPING for files).
GENERIC_FILE_MAPPING = (
    (GENERIC_READ, FILE_GENERIC_READ),
    (GENERIC_WRITE, FILE_GENERIC_WRITE),
    (GENERIC_EXECUTE, FILE_GENERIC_EXECUTE),
    (GENERIC_ALL, FILE_ALL_ACCESS),
)

#: The most bytes a UNICODE_STRING can describe: its Length and
#: MaximumLength are USHORT, and MaximumLength counts the terminator.
UNICODE_STRING_MAX_BYTES = 0xFFFF - 2

# File attribute bits a directory listing reports.
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400

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
    # AclSize is the allocation, not the used length: Windows may leave
    # unused space after the last ACE, and a valid descriptor with such
    # slack must decode (Codex review of ccb85ef, R3). An ACE running past
    # the allocation was refused above; bytes after the last ACE are not
    # an ACE and are not interpreted.
    if offset > size:
        raise ValueError("ACEs run past the ACL")
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
# Self-relative security descriptor codec
# ---------------------------------------------------------------------------

_SD_HEADER = struct.Struct("<BBHIIII")


def parse_security_descriptor(data: bytes, *, is_directory: bool,
                              sddl: str = "") -> SecurityState:
    """Decode the self-relative descriptor ``NtQuerySecurityObject`` returns.

    Strict, like :func:`parse_acl`: an offset outside the buffer, a
    descriptor that is not self-relative, or an owner or DACL that does
    not decode raises ValueError rather than producing a guess. Only the
    owner and the DACL are read; the group and SACL offsets are ignored.
    """
    if len(data) < _SD_HEADER.size:
        raise ValueError("security descriptor shorter than its header")
    revision, _sbz1, control, owner_offset, _group, _sacl, dacl_offset = (
        _SD_HEADER.unpack_from(data))
    if revision != SECURITY_DESCRIPTOR_REVISION:
        raise ValueError(f"unsupported security descriptor revision {revision}")
    if not control & SE_SELF_RELATIVE:
        raise ValueError("security descriptor is not self-relative")
    owner_sid = ""
    if owner_offset:
        if owner_offset + 8 > len(data):
            raise ValueError("owner SID runs past the descriptor")
        owner_length = 8 + 4 * data[owner_offset + 1]
        if owner_offset + owner_length > len(data):
            raise ValueError("owner SID runs past the descriptor")
        owner_sid = parse_sid(data[owner_offset:owner_offset + owner_length])
    dacl = None
    if control & SE_DACL_PRESENT and dacl_offset:
        if dacl_offset + _ACL_HEADER.size > len(data):
            raise ValueError("DACL runs past the descriptor")
        size = acl_size(data[dacl_offset:dacl_offset + _ACL_HEADER.size])
        if dacl_offset + size > len(data):
            raise ValueError("DACL runs past the descriptor")
        dacl = parse_acl(data[dacl_offset:dacl_offset + size])
    return SecurityState(owner_sid=owner_sid,
                         protected=bool(control & SE_DACL_PROTECTED),
                         dacl=dacl, is_directory=is_directory, sddl=sddl)


def build_owner_only_descriptor(owner_sid: str, *, directory: bool) -> bytes:
    """A self-relative descriptor carrying only the owner-only DACL, protected.

    This is what ``NtSetSecurityObject`` is handed with
    ``DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION``.
    The control word says protected as well, so both routes to the
    protected bit agree. No owner, group or SACL is carried: the call
    replaces the DACL of one object and touches nothing else about it.
    """
    acl = build_owner_only_acl(owner_sid, directory=directory)
    control = SE_DACL_PRESENT | SE_DACL_PROTECTED | SE_SELF_RELATIVE
    return _SD_HEADER.pack(SECURITY_DESCRIPTOR_REVISION, 0, control,
                           0, 0, 0, _SD_HEADER.size) + acl


# ---------------------------------------------------------------------------
# Directory listing codec (FILE_FULL_DIR_INFO)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DirectoryEntry:
    """One record from a directory handle's enumeration."""

    name: str
    attributes: int

    @property
    def is_directory(self) -> bool:
        return bool(self.attributes & FILE_ATTRIBUTE_DIRECTORY)

    @property
    def is_reparse_point(self) -> bool:
        return bool(self.attributes & FILE_ATTRIBUTE_REPARSE_POINT)


# FILE_FULL_DIR_INFO: NextEntryOffset at 0, FileAttributes at 56,
# FileNameLength (bytes) at 60, FileName (UTF-16-LE, unterminated) at 68.
_DIR_NEXT = struct.Struct("<I")
_DIR_ATTRIBUTES_OFFSET = 56
_DIR_NAME_LENGTH_OFFSET = 60
_DIR_NAME_OFFSET = 68


def parse_directory_listing(data: bytes) -> tuple[DirectoryEntry, ...]:
    """Decode one buffer of ``FILE_FULL_DIR_INFO`` records.

    The ``.`` and ``..`` entries are dropped. A name that is not a single
    path component (a separator, a NUL, an alternate-stream colon) is
    refused, because the platform layer opens each name relative to the
    directory it was listed in and a name that could resolve anywhere else
    must not reach that open. A record that runs past the buffer, or a
    chain that does not move forward, is refused rather than guessed at.
    """
    entries: list[DirectoryEntry] = []
    offset = 0
    seen: set[str] = set()
    while True:
        if offset + _DIR_NAME_OFFSET > len(data):
            raise ValueError("directory record runs past the buffer")
        next_offset = _DIR_NEXT.unpack_from(data, offset)[0]
        attributes = _DIR_NEXT.unpack_from(data, offset + _DIR_ATTRIBUTES_OFFSET)[0]
        name_length = _DIR_NEXT.unpack_from(data, offset + _DIR_NAME_LENGTH_OFFSET)[0]
        start = offset + _DIR_NAME_OFFSET
        if name_length % 2 or start + name_length > len(data):
            raise ValueError("directory entry name runs past the buffer")
        if next_offset and (next_offset < _DIR_NAME_OFFSET + name_length
                            or next_offset % 4):
            raise ValueError("directory record chain does not move forward")
        name = data[start:start + name_length].decode("utf-16-le")
        if name not in (".", ".."):
            if not name or any(ch in name for ch in "\\/\0:"):
                raise ValueError(f"directory entry is not a single component: {name!r}")
            folded = name.casefold()
            if folded in seen:
                raise ValueError(f"directory entry listed twice: {name!r}")
            seen.add(folded)
            entries.append(DirectoryEntry(name, attributes))
        if not next_offset:
            return tuple(entries)
        offset += next_offset


# ---------------------------------------------------------------------------
# Judgment
# ---------------------------------------------------------------------------


def accepted_owners(caller_sid: str, default_owner_sid: str | None = None
                    ) -> frozenset[str]:
    """The SIDs an object may be owned by and still be the caller's own.

    The token user always. The token's default owner (TokenOwner) as
    well, but only when it is the Administrators group: that is what an
    elevated administrator's freshly created objects are owned by, and
    Administrators is already a principal the owner-only DACL admits. Any
    other default owner is ignored (Codex review of ccb85ef..2e9ed0f, B2):
    a token whose default owner is some shared group would otherwise make
    every object that group owns look like the caller's own, and that
    group's members could read what is inside. ValueError if either is
    not a SID.
    """
    owners = {canonical_sid(caller_sid)}
    if default_owner_sid:
        default_owner = canonical_sid(default_owner_sid)
        if default_owner == SID_ADMINISTRATORS:
            owners.add(default_owner)
    return frozenset(owners)


def map_generic_rights(mask: int) -> int:
    """``mask`` with each generic right replaced by its file-specific rights.

    A DACL entry may carry generic bits, specific bits, or both; the
    kernel maps the generic ones before it checks access, and so must a
    judgment that reads the same entry.
    """
    mapped = mask
    for generic, specific in GENERIC_FILE_MAPPING:
        if mask & generic:
            mapped = (mapped & ~generic) | specific
    return mapped


def grants_effective_access(mask: int) -> bool:
    """Whether the accumulated rights let the holder read and write the object."""
    return map_generic_rights(mask) & REQUIRED_OWNER_ACCESS == REQUIRED_OWNER_ACCESS


def encode_object_name(name: str) -> bytes:
    """``name`` as the UTF-16-LE bytes a UNICODE_STRING's Length counts.

    Length is a byte count of UTF-16 code units, not of Python code
    points: a supplementary character is one code point and four bytes.
    ValueError for a name longer than a UNICODE_STRING can describe, or
    one that is not valid text.
    """
    encoded = name.encode("utf-16-le")
    if len(encoded) > UNICODE_STRING_MAX_BYTES:
        raise ValueError("object name longer than a UNICODE_STRING can describe")
    return encoded


def judge_security(state: SecurityState, caller_sid: str, *,
                   allow_inherited: bool = False,
                   default_owner_sid: str | None = None) -> list[str]:
    """Every reason ``state`` is not owner-only for ``caller_sid``. Empty is a pass.

    The rules, in the order they are checked:

    * The object must be owned by the caller: the token user, or the
      token's default owner when ``default_owner_sid`` gives one (see the
      module docstring). Not "an administrator", not "somebody with OWNER
      RIGHTS": an object another account owns inside the caller's store is
      that account's to read, whatever the DACL says, and is refused.
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
    * The caller must actually appear, directly or as OWNER RIGHTS, and
      the entries that apply to this object (not inherit-only ones) must
      together grant at least generic read and write. An entry with an
      empty mask, or one that applies only to future children, names the
      caller without admitting the caller (Codex review of ccb85ef, R4).
    """
    try:
        caller = canonical_sid(caller_sid)
        owners = accepted_owners(caller_sid, default_owner_sid)
    except ValueError:
        return ["caller identity unknown"]
    problems: list[str] = []
    if state.owner_sid not in owners:
        problems.append(f"owned by {state.owner_sid or 'nobody'}, not the caller")
    if state.dacl is None:
        problems.append("no DACL (everyone has every access)")
        return problems
    if not state.dacl:
        problems.append("empty DACL")
    if not allow_inherited and not state.protected:
        problems.append("DACL inherits from the parent")
    owner_named = False
    effective = 0
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
            owner_named = True
            if not ace.flags & INHERIT_ONLY_ACE:
                effective |= ace.mask
        elif sid in (SID_SYSTEM, SID_ADMINISTRATORS):
            continue
        else:
            problems.append(f"{sid} has access")
    if state.dacl:
        if not owner_named:
            problems.append("the caller has no entry")
        elif not grants_effective_access(effective):
            problems.append("the caller's entries grant no effective access")
    return problems


def is_exactly_owner_only(state: SecurityState, caller_sid: str, *,
                          default_owner_sid: str | None = None) -> bool:
    """Whether ``state`` is exactly the DACL :func:`build_owner_only_acl` writes.

    The read-back after protecting an object is held to this, stricter
    than :func:`judge_security`: what was written is what must be there,
    and nothing else, on an object the caller owns.
    """
    if judge_security(state, caller_sid, default_owner_sid=default_owner_sid) != []:
        return False
    return bool(state.protected and state.dacl is not None
                and tuple(state.dacl) == owner_only_aces(
                    caller_sid, directory=state.is_directory))

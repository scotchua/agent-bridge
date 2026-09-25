"""Admission to the Codex execution lane under codex-bridge's CLI promotion.

codex-bridge (firm-claude-plugins, ``firm-tools/skills/codex-bridge/upgrade``)
promotes new Codex CLI releases by switching the shared launchers
(``~/.local/bin/codex`` and the nvm prefix's ``bin/codex``) to a candidate
install slot, running canaries, and committing a promotion record. During that
window the launchers point at code that has not passed yet. The execution lane
runs ``codex`` through the same launcher, so it must take part in the same
admission protocol or a real task could run the candidate (spec v5, section 3:
~/Claude/team-state/tasks/codex-cli-auto-upgrade/SPEC.md).

The protocol, from this side:

1. ``flock(LOCK_SH | LOCK_NB)`` on ``admission.lock``, held until the task and
   every Codex child it started have finished. The controller drains with
   ``LOCK_EX``, so a held shared lock is what makes it wait for us.
2. Only then read ``gate.json``. Present means maintenance: refuse. Present but
   unreadable also refuses; a gate that cannot be read is never an open gate.
3. Verify the promotion record, if there is one, and return the resolved
   ``codex.js`` inside the verified slot. The caller executes that path, not
   the launcher name, so a launcher swapped after the check is not what runs.

Transition, stated plainly: until codex-bridge's bootstrap creates the
promotion directory there is nothing to take part in, and admission reports
``not_installed`` and admits exactly as before this module existed. Once the
directory exists, every missing or malformed piece of it is a refusal. With a
directory but no record yet (bootstrap in progress or undone), the gate still
applies and admission reports ``no_record``.

codex-bridge's ``promotion.py`` owns the record schema and writes it. This
module is an independent reader: it checks the exact top-level key set, the
``record_id`` over the canonical encoding, the launcher it was given, the
Node runtime that launcher's shebang will find, and the full slot tree
digest, recomputed on every admission with no cache (spec v5 N3). Both
implementations are pinned to the same test vector
(``tests/fixtures/codex_promotion_vector.json``) so they cannot drift apart
silently.

Same-user tampering is out of scope by the owner's decision (spec v5, threat
model): these checks catch a broken or half-finished promotion, not an
attacker running as the user.
"""
from __future__ import annotations

import contextlib, hashlib, json, os, stat
from dataclasses import dataclass, field
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no promotion controller
    fcntl = None

#: Overrides the promotion directory. Tests set it so they never read the
#: operator's real promotion state; production leaves it unset.
PROMOTION_DIR_ENV="AGENT_BRIDGE_CODEX_PROMOTION_DIR"

RECORD_KEYS=frozenset({"schema_version","record_id","predecessor_id","version","slot_path",
                       "slot_tree_digest","launchers","canary","promoted_at","promoted_by",
                       "transaction_id"})
LAUNCHER_KEYS=frozenset({"name","path","target","node"})
NODE_KEYS=frozenset({"path","sha256","version"})
PROMOTED_BY=frozenset({"auto","supervised","bootstrap","rehearsal","rollback"})
GATE_KEYS=frozenset({"transaction_id","state","opened_at","controller_pid"})
MAX_STATE_FILE_BYTES=1<<20
_READ_CHUNK=1<<20


class AdmissionRefused(RuntimeError):
    """Operator-safe refusal text; never carries file contents."""


@dataclass(frozen=True)
class Admission:
    state:str  # not_installed | no_record | promoted
    exec_path:Path
    record_id:str|None=None
    version:str|None=None
    slot_tree_digest:str|None=None
    notes:tuple[str,...]=field(default_factory=tuple)

    def receipt(self)->dict:
        return {"state":self.state,"exec_path":str(self.exec_path),"record_id":self.record_id,
                "version":self.version,"slot_tree_digest":self.slot_tree_digest}


def default_promotion_dir()->Path:
    override=os.environ.get(PROMOTION_DIR_ENV)
    return Path(override) if override else Path.home()/".codex-bridge"/"promotion"


# ---------------------------------------------------------------- digests

def canonical_bytes(value)->bytes:
    """The encoding record_id is computed over: sorted keys, no whitespace, UTF-8."""
    _reject_floats(value)
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8")

def record_id_of(record:dict)->str:
    return hashlib.sha256(canonical_bytes({k:v for k,v in record.items() if k!="record_id"})).hexdigest()

def _reject_floats(value):
    if isinstance(value,float): raise AdmissionRefused("promotion record carries a float")
    if isinstance(value,dict):
        for k,v in value.items():
            if not isinstance(k,str): raise AdmissionRefused("promotion record carries a non-string key")
            _reject_floats(v)
    elif isinstance(value,list):
        for v in value: _reject_floats(v)

def _digest_mode(st)->str:
    # Write bits are masked out: the controller makes the slot read-only after
    # install, and that chmod must not change the digest. Every other mode
    # change (an execute bit, setuid) still does.
    return format(stat.S_IMODE(st.st_mode)&0o7555,"04o")

def _file_sha256(path:str,st)->str:
    flags=os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)|getattr(os,"O_NONBLOCK",0)
    fd=os.open(path,flags)
    try:
        opened=os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev,opened.st_ino)!=(st.st_dev,st.st_ino):
            raise AdmissionRefused("slot file changed while it was being hashed")
        h=hashlib.sha256()
        while chunk:=os.read(fd,_READ_CHUNK): h.update(chunk)
        return h.hexdigest()
    finally:
        os.close(fd)

def tree_digest(slot:Path)->str:
    """sha256 over every entry under ``slot``, walked without following links.

    Entries sort by their slash-separated relative path, compared as UTF-8
    bytes. Each contributes one line, fields separated by NUL:

      file:     ``f``, path, mode, sha256 of contents
      dir:      ``d``, path, mode
      symlink:  ``l``, path, link text

    Mode is four octal digits with the write bits cleared. The slot root itself
    is not an entry. A symlink must be relative and resolve inside the slot;
    any other file type (FIFO, socket, device) is refused.
    """
    root=os.path.realpath(slot)
    entries=[]
    def walk(directory:str,rel:str):
        with os.scandir(directory) as it:
            children=sorted(it,key=lambda e:e.name.encode("utf-8","surrogateescape"))
        for entry in children:
            path=entry.path; relpath=f"{rel}/{entry.name}" if rel else entry.name
            st=os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                text=os.readlink(path)
                if os.path.isabs(text): raise AdmissionRefused(f"slot symlink is absolute: {relpath}")
                resolved=os.path.realpath(path)
                if os.path.commonpath([root,resolved])!=root or not os.path.exists(resolved):
                    raise AdmissionRefused(f"slot symlink escapes the slot: {relpath}")
                entries.append(("l",relpath,text))
            elif stat.S_ISDIR(st.st_mode):
                entries.append(("d",relpath,_digest_mode(st))); walk(path,relpath)
            elif stat.S_ISREG(st.st_mode):
                entries.append(("f",relpath,_digest_mode(st),_file_sha256(path,st)))
            else:
                raise AdmissionRefused(f"slot holds an unsupported file type: {relpath}")
    root_st=os.lstat(slot)
    if not stat.S_ISDIR(root_st.st_mode): raise AdmissionRefused("promoted slot is not a directory")
    walk(str(slot),"")
    entries.sort(key=lambda e:e[1].encode("utf-8","surrogateescape"))
    h=hashlib.sha256()
    for e in entries: h.update("\0".join(e).encode("utf-8","surrogateescape")+b"\n")
    return h.hexdigest()


# ---------------------------------------------------------------- state files

def _owned_private(st,what:str,*,mode_mask:int):
    if hasattr(os,"getuid") and st.st_uid!=os.getuid(): raise AdmissionRefused(f"{what} is not owned by this user")
    if stat.S_IMODE(st.st_mode)&mode_mask: raise AdmissionRefused(f"{what} is readable or writable by others")

def _read_state_json(path:Path,what:str):
    flags=os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)|getattr(os,"O_NONBLOCK",0)
    try: fd=os.open(path,flags)
    except OSError as exc: raise AdmissionRefused(f"{what} could not be opened") from exc
    try:
        st=os.fstat(fd)
        if not stat.S_ISREG(st.st_mode): raise AdmissionRefused(f"{what} is not a regular file")
        _owned_private(st,what,mode_mask=0o077)
        if st.st_size>MAX_STATE_FILE_BYTES: raise AdmissionRefused(f"{what} is too large")
        raw=b""
        while chunk:=os.read(fd,_READ_CHUNK):
            raw+=chunk
            if len(raw)>MAX_STATE_FILE_BYTES: raise AdmissionRefused(f"{what} is too large")
    finally:
        os.close(fd)
    try: return json.loads(raw.decode("utf-8"),parse_float=_float_refused)
    except (UnicodeDecodeError,ValueError) as exc: raise AdmissionRefused(f"{what} does not parse") from exc

def _float_refused(text):
    raise ValueError("float")

def _is_hex64(value)->bool:
    return isinstance(value,str) and len(value)==64 and all(c in "0123456789abcdef" for c in value)


def _check_gate(promotion_dir:Path):
    gate=promotion_dir/"gate.json"
    try: os.lstat(gate)
    except FileNotFoundError: return
    try: value=_read_state_json(gate,"maintenance gate")
    except AdmissionRefused as exc:
        raise AdmissionRefused(f"maintenance gate is unreadable, so admission stays closed ({exc})") from None
    if not isinstance(value,dict) or set(value)!=GATE_KEYS or not isinstance(value.get("transaction_id"),str):
        raise AdmissionRefused("maintenance gate is malformed, so admission stays closed")
    raise AdmissionRefused(f"maintenance in progress (transaction {value['transaction_id'][:64]})")


def _verify_record(promotion_dir:Path,codex_bin:Path,node_bin:str|None)->Admission:
    record_path=promotion_dir/"promoted-cli.json"
    try: os.lstat(record_path)
    except FileNotFoundError: return Admission(state="no_record",exec_path=codex_bin)
    record=_read_state_json(record_path,"promotion record")
    if not isinstance(record,dict) or set(record)!=RECORD_KEYS: raise AdmissionRefused("promotion record has the wrong fields")
    if record["schema_version"]!=1: raise AdmissionRefused("promotion record schema version is not 1")
    if not _is_hex64(record["record_id"]) or record_id_of(record)!=record["record_id"]:
        raise AdmissionRefused("promotion record id does not match its content")
    if record["promoted_by"] not in PROMOTED_BY: raise AdmissionRefused("promotion record has an unknown promoter")
    version=record["version"]
    if not isinstance(version,str) or not all(p.isdigit() for p in version.split(".")) or version.count(".")!=2:
        raise AdmissionRefused("promotion record version is not a stable x.y.z version")
    if not _is_hex64(record["slot_tree_digest"]): raise AdmissionRefused("promotion record slot digest is malformed")
    slot=Path(record["slot_path"]) if isinstance(record["slot_path"],str) else None
    if slot is None or not slot.is_absolute(): raise AdmissionRefused("promotion record slot path is not absolute")
    launchers=record["launchers"]
    if not isinstance(launchers,list) or not launchers: raise AdmissionRefused("promotion record lists no launchers")
    match=None
    for entry in launchers:
        if not isinstance(entry,dict) or set(entry)!=LAUNCHER_KEYS or not isinstance(entry["node"],dict) \
                or set(entry["node"])!=NODE_KEYS:
            raise AdmissionRefused("promotion record launcher entry has the wrong fields")
        if entry["path"]==str(codex_bin): match=entry
    if match is None: raise AdmissionRefused("the Codex executable is not a launcher named in the promotion record")
    try: link=os.readlink(codex_bin)
    except OSError as exc: raise AdmissionRefused("the Codex launcher is not a symlink") from exc
    if link!=match["target"]: raise AdmissionRefused("the Codex launcher does not point at the promoted slot")
    target=Path(os.path.realpath(codex_bin))
    real_slot=os.path.realpath(slot)
    if os.path.commonpath([real_slot,str(target)])!=real_slot:
        raise AdmissionRefused("the Codex launcher resolves outside the promoted slot")
    if tree_digest(slot)!=record["slot_tree_digest"]: raise AdmissionRefused("the promoted slot no longer matches its digest")
    _verify_node(match["node"],node_bin)
    return Admission(state="promoted",exec_path=target,record_id=record["record_id"],version=version,
                     slot_tree_digest=record["slot_tree_digest"])


def _verify_node(expected:dict,node_bin:str|None):
    # codex.js starts with ``#!/usr/bin/env node``, so the runtime is whatever
    # ``node`` the child's PATH finds. The record names the one the canaries ran.
    if not node_bin: raise AdmissionRefused("no node runtime on the task PATH")
    real=os.path.realpath(node_bin)
    if real!=expected["path"]: raise AdmissionRefused("the node runtime differs from the one the canaries ran")
    try: st=os.stat(real)
    except OSError as exc: raise AdmissionRefused("the node runtime is unreadable") from exc
    if _file_sha256(real,st)!=expected["sha256"]:
        raise AdmissionRefused("the node runtime changed since the canaries ran")


@contextlib.contextmanager
def admit(codex_bin:Path,*,promotion_dir:Path|None=None,node_bin:str|None=None):
    """Hold execution-lane admission for the body of the ``with`` block.

    ``codex_bin`` is the launcher path as given (not resolved). ``node_bin`` is
    the ``node`` the task's PATH resolves to. Yields an ``Admission`` whose
    ``exec_path`` is what the caller must execute. Raises ``AdmissionRefused``.
    """
    promotion_dir=promotion_dir or default_promotion_dir()
    if not codex_bin.is_absolute(): raise AdmissionRefused("Codex executable path must be absolute")
    try: dir_st=os.lstat(promotion_dir)
    except FileNotFoundError:
        yield Admission(state="not_installed",exec_path=codex_bin); return
    if not stat.S_ISDIR(dir_st.st_mode): raise AdmissionRefused("promotion directory is not a directory")
    _owned_private(dir_st,"promotion directory",mode_mask=0o077)
    if fcntl is None: raise AdmissionRefused("promotion admission needs POSIX file locks")
    lock=promotion_dir/"admission.lock"
    try: lock_st=os.lstat(lock)
    except FileNotFoundError: raise AdmissionRefused("admission lock is missing, so admission stays closed") from None
    if not stat.S_ISREG(lock_st.st_mode): raise AdmissionRefused("admission lock is not a regular file")
    _owned_private(lock_st,"admission lock",mode_mask=0o022)
    fd=os.open(lock,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
    try:
        opened=os.fstat(fd)
        if (opened.st_dev,opened.st_ino)!=(lock_st.st_dev,lock_st.st_ino):
            raise AdmissionRefused("admission lock was replaced while it was opened")
        try: fcntl.flock(fd,fcntl.LOCK_SH|fcntl.LOCK_NB)
        except BlockingIOError: raise AdmissionRefused("maintenance in progress (admission lock held)") from None
        _check_gate(promotion_dir)
        yield _verify_record(promotion_dir,codex_bin,node_bin)
    finally:
        os.close(fd)  # closing the descriptor releases the shared lock

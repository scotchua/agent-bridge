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

There is no transition bypass. The lock is taken on every admission, before
codex-bridge's bootstrap has run too: if the promotion directory does not
exist yet, admission creates it (``mkdir`` 0700) and then its lock file
(``O_CREAT`` without truncation, the same way the controller does). So a task
that starts just before bootstrap already holds the shared lock that
bootstrap's drain waits for. The lock is created only together with its
directory. A directory that exists without its lock refuses and needs repair:
recreating the lock there could leave a running task holding the old inode
while the controller drains a new one. With no record yet (bootstrap not run,
or undone), admission reports ``no_record`` and runs the launcher it was
given, exactly as before.

The promotion directory and the launcher paths are under ``$HOME``, the same
home this lane already derives its task root and ``CODEX_HOME`` from, and
admission refuses any process whose ``HOME`` is not the account's home in the
password database. A worker started with another ``HOME`` would otherwise
lock and read an empty directory of its own, where no gate is ever written.
There is no environment override for the directory either. Tests inject one
in code (``promotion_dir=``, or by patching ``managed_home`` and
``account_home``); a test that runs the lane as a subprocess under an
isolated ``HOME`` goes through ``tests/fixtures/codex_task_isolated_home.py``.

Once a record exists, the task's executable must be one of the launchers it
names, and the record must name exactly the three launchers spec v5 section 1
defines: ``local`` (``~/.local/bin/codex``), ``nvm`` (the nvm prefix's
``bin/codex``) and ``peer`` (``~/.codex-cli/peer/codex``). All three are
verified on every admission, not only the one this task was given (the spec's
mixed-install rule). Nothing else runs on a real task while a record exists.

codex-bridge's ``promotion.py`` owns the record schema and writes it. This
module is an independent reader: it checks the exact top-level key set, the
``record_id`` over the canonical encoding, the complete launcher set, every
launcher's target and recorded Node runtime, the Node this task's PATH will
find, the slot's package version, and the full slot tree digest, recomputed on every
admission with no cache (spec v5 N3). It does not judge the ``canary``
field: that a record passed its canaries is what codex-bridge's
``promote.py`` attests by committing it, and ``record_id`` only binds what
the record says. Both
implementations are pinned to the same test vector
(``tests/fixtures/codex_promotion_vector.json``) so they cannot drift apart
silently.

Same-user tampering is out of scope by the owner's decision (spec v5, threat
model): these checks catch a broken or half-finished promotion, not an
attacker running as the user.
"""
from __future__ import annotations

import contextlib, hashlib, json, os, re, stat
from dataclasses import dataclass, field
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no promotion controller
    fcntl = None
try:
    import pwd
except ImportError:  # pragma: no cover
    pwd = None

RECORD_KEYS=frozenset({"schema_version","record_id","predecessor_id","version","slot_path",
                       "slot_tree_digest","launchers","canary","promoted_at","promoted_by",
                       "transaction_id"})
LAUNCHER_KEYS=frozenset({"name","path","target","node"})
NODE_KEYS=frozenset({"path","sha256","version"})
PROMOTED_BY=frozenset({"auto","supervised","bootstrap","rehearsal","rollback"})
LAUNCHER_NAMES=frozenset({"local","nvm","peer"})
_NVM_NODE_VERSION=re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
GATE_KEYS=frozenset({"transaction_id","state","opened_at","controller_pid"})
MAX_STATE_FILE_BYTES=1<<20
_READ_CHUNK=1<<20


class AdmissionRefused(RuntimeError):
    """Operator-safe refusal text; never carries file contents."""


@dataclass(frozen=True)
class Admission:
    state:str  # no_record | promoted
    exec_path:Path
    record_id:str|None=None
    version:str|None=None
    slot_tree_digest:str|None=None
    notes:tuple[str,...]=field(default_factory=tuple)

    def receipt(self)->dict:
        return {"state":self.state,"exec_path":str(self.exec_path),"record_id":self.record_id,
                "version":self.version,"slot_tree_digest":self.slot_tree_digest}

    def check_reported_version(self,output:str):
        """Refuse unless a promoted executable reports the record's version.

        The caller runs ``--version`` on ``exec_path`` under the held lock and
        passes its stdout here. Every launcher resolves to the same verified
        ``codex.js`` under a verified Node, so this is the one that can differ.
        """
        if self.state=="promoted" and output.strip()!=f"codex-cli {self.version}":
            raise AdmissionRefused("the Codex executable reports a version other than the promoted one")


def managed_home()->Path:
    """The home whose promotion directory and launchers admission uses."""
    return Path.home()

def account_home()->Path:
    """The account's home from the password database; ``$HOME`` can differ per process."""
    if pwd is None: return Path.home()  # pragma: no cover - no promotion controller there
    return Path(pwd.getpwuid(os.getuid()).pw_dir)

def default_promotion_dir()->Path:
    return managed_home()/".codex-bridge"/"promotion"


def _check_home():
    # A worker started with another HOME would take a lock nobody drains and
    # read no gate, whatever executable it was given.
    if os.path.realpath(managed_home())!=os.path.realpath(account_home()):
        raise AdmissionRefused("HOME is not the account home, so this process cannot see the promotion state")


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
    # The full mode. The controller makes the slot read-only before it
    # computes the digest, so a write bit added later changes the digest.
    return format(stat.S_IMODE(st.st_mode),"04o")

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

    Mode is the full permission mode as four octal digits. The slot root itself
    is not an entry; admission separately refuses a root with any write bit.
    A symlink must be relative and resolve inside the slot; any other file
    type (FIFO, socket, device) is refused.
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
    for entry in launchers:
        if not isinstance(entry,dict) or set(entry)!=LAUNCHER_KEYS or not isinstance(entry["node"],dict) \
                or set(entry["node"])!=NODE_KEYS or not all(isinstance(entry[k],str) for k in ("name","path","target")):
            raise AdmissionRefused("promotion record launcher entry has the wrong fields")
    _check_launcher_set(launchers)
    real_slot=os.path.realpath(slot)
    root_st=os.lstat(slot) if os.path.lexists(slot) else None
    if root_st is None or not stat.S_ISDIR(root_st.st_mode): raise AdmissionRefused("the promoted slot is missing")
    if stat.S_IMODE(root_st.st_mode)&0o222: raise AdmissionRefused("the promoted slot is writable")
    if tree_digest(slot)!=record["slot_tree_digest"]: raise AdmissionRefused("the promoted slot no longer matches its digest")
    if _slot_package_version(slot)!=version:
        raise AdmissionRefused("the promoted slot's package version differs from the record")
    # Every managed launcher, not just ours: a half-switched pair is exactly
    # what a crashed maintenance run would leave behind.
    ours=None
    for entry in launchers:
        name=entry["name"][:32]
        try: link=os.readlink(entry["path"])
        except OSError as exc: raise AdmissionRefused(f"managed launcher {name} is not a symlink") from exc
        if link!=entry["target"]: raise AdmissionRefused(f"managed launcher {name} does not point at the promoted slot")
        resolved=os.path.realpath(entry["path"])
        if os.path.commonpath([real_slot,resolved])!=real_slot:
            raise AdmissionRefused(f"managed launcher {name} resolves outside the promoted slot")
        _verify_node_file(entry["node"],name)
        if entry["path"]==str(codex_bin): ours=entry
    if ours is None: raise AdmissionRefused("the Codex executable is not a launcher named in the promotion record")
    _verify_task_node(ours["node"],node_bin)
    return Admission(state="promoted",exec_path=Path(os.path.realpath(codex_bin)),record_id=record["record_id"],
                     version=version,slot_tree_digest=record["slot_tree_digest"])


def _check_launcher_set(launchers:list):
    # Exactly the three launchers of spec v5 section 1, each once, at its own
    # path. A record naming fewer would hide a launcher left on another slot.
    names=[e["name"] for e in launchers]
    if len(names)!=len(LAUNCHER_NAMES) or set(names)!=LAUNCHER_NAMES:
        raise AdmissionRefused("promotion record does not list exactly the local, nvm and peer launchers")
    by={e["name"]:e["path"] for e in launchers}
    home=managed_home()
    if by["local"]!=str(home/".local"/"bin"/"codex"): raise AdmissionRefused("promotion record local launcher path is wrong")
    if by["peer"]!=str(home/".codex-cli"/"peer"/"codex"): raise AdmissionRefused("promotion record peer launcher path is wrong")
    nvm=Path(by["nvm"])
    if nvm.parent.parent.parent!=home/".nvm"/"versions"/"node" or nvm.parent.name!="bin" or nvm.name!="codex" \
            or not _NVM_NODE_VERSION.fullmatch(nvm.parent.parent.name):
        raise AdmissionRefused("promotion record nvm launcher path is wrong")


def _slot_package_version(slot:Path):
    p=slot/"node_modules"/"@openai"/"codex"/"package.json"
    try: st=os.lstat(p)
    except OSError as exc: raise AdmissionRefused("the promoted slot has no Codex package.json") from exc
    if not stat.S_ISREG(st.st_mode) or st.st_size>MAX_STATE_FILE_BYTES:
        raise AdmissionRefused("the promoted slot's package.json is not a regular file")
    try: return json.loads(p.read_bytes().decode("utf-8")).get("version")
    except (UnicodeDecodeError,ValueError,AttributeError) as exc:
        raise AdmissionRefused("the promoted slot's package.json does not parse") from exc


def _verify_node_file(expected:dict,name:str):
    try: st=os.stat(expected["path"])
    except (OSError,TypeError) as exc: raise AdmissionRefused(f"the node runtime for launcher {name} is missing") from exc
    if not stat.S_ISREG(st.st_mode) or _file_sha256(os.path.realpath(expected["path"]),st)!=expected["sha256"]:
        raise AdmissionRefused(f"the node runtime for launcher {name} changed since the canaries ran")


def _verify_task_node(expected:dict,node_bin:str|None):
    # codex.js starts with ``#!/usr/bin/env node``, so the runtime is whatever
    # ``node`` the child's PATH finds. It must be the one recorded for this
    # launcher's context (its bytes were checked in _verify_node_file).
    if not node_bin: raise AdmissionRefused("no node runtime on the task PATH")
    if os.path.realpath(node_bin)!=expected["path"]:
        raise AdmissionRefused("the node runtime differs from the one the canaries ran")


@contextlib.contextmanager
def admit(codex_bin:Path,*,promotion_dir:Path|None=None,node_bin:str|None=None):
    """Hold execution-lane admission for the body of the ``with`` block.

    ``codex_bin`` is the launcher path as given (not resolved). ``node_bin`` is
    the ``node`` the task's PATH resolves to. Yields an ``Admission`` whose
    ``exec_path`` is what the caller must execute. Raises ``AdmissionRefused``.
    """
    promotion_dir=promotion_dir or default_promotion_dir()
    if not codex_bin.is_absolute(): raise AdmissionRefused("Codex executable path must be absolute")
    if fcntl is None: raise AdmissionRefused("promotion admission needs POSIX file locks")
    _check_home()
    try: promotion_dir.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    except OSError as exc: raise AdmissionRefused("promotion directory could not be created") from exc
    try: os.mkdir(promotion_dir,0o700); created=True
    except FileExistsError: created=False
    except OSError as exc: raise AdmissionRefused("promotion directory could not be created") from exc
    dir_st=os.lstat(promotion_dir)
    if not stat.S_ISDIR(dir_st.st_mode): raise AdmissionRefused("promotion directory is not a directory")
    _owned_private(dir_st,"promotion directory",mode_mask=0o077)
    lock=promotion_dir/"admission.lock"
    # Only the process that created the directory creates the lock, with
    # O_CREAT but no O_TRUNC or O_EXCL, so a controller racing it opens the
    # same inode. Nobody ever replaces the lock, so that inode is permanent.
    flags=os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)|(os.O_CREAT if created else 0)
    try: fd=os.open(lock,flags,0o600)
    except FileNotFoundError:
        raise AdmissionRefused("admission lock is missing, so the promotion directory needs repair") from None
    except OSError as exc: raise AdmissionRefused("admission lock is not a regular file") from exc
    try:
        opened=os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode): raise AdmissionRefused("admission lock is not a regular file")
        _owned_private(opened,"admission lock",mode_mask=0o022)
        try: now=os.lstat(lock)
        except FileNotFoundError: raise AdmissionRefused("admission lock was removed while it was opened") from None
        if (opened.st_dev,opened.st_ino)!=(now.st_dev,now.st_ino):
            raise AdmissionRefused("admission lock was replaced while it was opened")
        try: fcntl.flock(fd,fcntl.LOCK_SH|fcntl.LOCK_NB)
        except BlockingIOError: raise AdmissionRefused("maintenance in progress (admission lock held)") from None
        _check_gate(promotion_dir)
        yield _verify_record(promotion_dir,codex_bin,node_bin)
    finally:
        os.close(fd)  # closing the descriptor releases the shared lock

"""Bounded Codex subscription implementation lane.

Mirrors `claude_task.py`: a disposable git worktree, one generation turn, an
unapplied patch that is independently reapplied and verified against a fresh
worktree, and this harness's own bounded verification commands. It never
applies, commits, pushes, or merges, and it never falls back to a paid API
key: `codex login status` must report a ChatGPT-authenticated isolated
`CODEX_HOME` before any turn runs.

Differences from the Claude lane, stated rather than left implicit:

1. Codex's own OS-level sandbox is the write boundary here, not a tool
   allowlist. `-s workspace-write -C <worktree>` is Codex's documented
   mechanism for confining writes to one directory, and
   `sandbox_workspace_write.network_access` is pinned to Boolean `false` so a
   drifted default cannot open network access mid-run. Unlike the Claude
   lane (`--tools Read,Grep,Glob,Edit,Write`, no shell), Codex keeps its own
   shell tool inside that sandbox: confinement here is exactly the boundary
   the vendor documents for `workspace-write`, no more, and no broader claim
   is made.
2. `--ignore-user-config` and `--ignore-rules` stop `$CODEX_HOME/config.toml`
   and `.rules` from loading, but Codex still discovers `AGENTS.md` by
   walking upward from its working directory (measured in
   `docs/verified-cli-behaviour.md`). The worktree itself is the caller's own
   repository and may legitimately carry `AGENTS.md`/`CLAUDE.md`; this harness
   does not gate that. What it does gate is everything *above* the disposable
   per-job directory this harness controls (the task root and its ancestors),
   walked for the same contaminant set the consultation backend refuses on,
   before any Codex invocation.
3. The isolated `CODEX_HOME` defaults to the same path `config/broker.json`
   documents for consultation (`~/.agent-bridge/codex-home`), so one
   `codex login` covers both lanes. This harness never copies credentials
   into it, never writes to it beyond what Codex itself writes, and never
   falls back to the shared desktop `~/.codex` home; a missing or
   logged-out isolated home fails the job closed with that fact stated
   plainly rather than silently trying the default home.
4. Read confinement is not claimed. `-s workspace-write` restricts writes,
   not reads, identically to the consultation peer's documented limitation
   (see the README's "Honest limits"). The controls that actually apply are
   the refusal of client-derived classifications, the disposable per-job
   worktree, and the ancestor instruction-file walk above; none of them is a
   filesystem read boundary, and none is claimed as one here.

Verified against the same codex-cli 0.147.0 facts recorded in
`docs/verified-cli-behaviour.md` and `backends/codex_backend.py`: the session
identifier is `thread_id`, carried on the `thread.started` JSONL event; the
final message is read from the documented `--output-last-message` file
channel; and the prompt goes on stdin for a first turn (`codex exec` reads
stdin when given no prompt argument).
"""
from __future__ import annotations

import argparse, hashlib, json, os, platform, shutil, stat, subprocess, sys, tempfile, time, uuid
from pathlib import Path
try:
    from .. import runner, preflight, store
    from ..platform import platform as agent_platform
    from ..orchestration import windows_privacy as wpv
    from ..errors import BrokerError
    from . import codex_promotion, hostenv, verify_policy
except ImportError:  # The orchestration worker invokes this file directly.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from agent_bridge import runner, preflight, store
    from agent_bridge.platform import platform as agent_platform
    from agent_bridge.orchestration import windows_privacy as wpv
    from agent_bridge.errors import BrokerError
    from agent_bridge.execution import codex_promotion, hostenv, verify_policy

class TaskError(RuntimeError): pass

#: Longest piece of Codex's own error text folded into a TaskError message.
ERROR_MESSAGE_DETAIL_LIMIT=200

def _with_error_detail(message:str,error_messages:list[str])->str:
    """Fold Codex's own first error/turn.failed message into a fixed
    TaskError message, bounded and printable-only.

    A bare exit status told an operator nothing about *why* (the same defect
    claude_task carries the identical fix for). ``error_messages`` already
    comes from ``_parse_events`` reading structured JSON event fields, not
    raw output, so this only bounds and sanitizes text Codex itself reported
    as the reason; ``message`` alone is returned when there is none."""
    if not error_messages: return message
    cleaned="".join(ch for ch in error_messages[0] if ch.isprintable())[:ERROR_MESSAGE_DETAIL_LIMIT]
    return f"{message}: {cleaned}" if cleaned else message

ALLOWED_CLASSIFICATIONS={"synthetic","public","internal_nonclient"}
ALLOWED_VERIFY_PROGRAMS=verify_policy.ALLOWED_VERIFY_PROGRAMS
MAX_BRIEF_BYTES=100_000; MAX_STREAM_BYTES=2_000_000
DEFAULT_TASK_ROOT=Path.home()/".agent-bridge"/"execution"
DEFAULT_CODEX_HOME=Path.home()/".agent-bridge"/"codex-home"
_GIT_BIN:Path|None=None

def _git_bin()->Path:
    """The host's git, resolved once and named in the refusal when absent.

    Same defect as the Claude lane carried: a constant pointing at the
    standalone macOS Command Line Tools, so the first git call on any other
    host reported only "command spawn failed".
    """
    global _GIT_BIN
    if _GIT_BIN is None:
        try: _GIT_BIN=hostenv.resolve_git()
        except hostenv.HostCapabilityError as exc: raise TaskError(str(exc)) from None
    return _GIT_BIN

def _run(argv:list[str],*,cwd:Path,env:dict[str,str],timeout:int,input_bytes:bytes|None=None):
    """Use the bridge's measured streaming caps and bounded post-kill drain."""
    r=runner.run(argv,cwd=str(cwd),env=env,stdin_data=(input_bytes or b"").decode("utf-8"),timeout=timeout,
                 grace=2,stdout_cap=MAX_STREAM_BYTES,stderr_cap=MAX_STREAM_BYTES)
    if r.spawn_failed: raise TaskError(_spawn_detail(argv))
    if r.timed_out: raise TaskError(f"command timed out after {timeout}s")
    if r.cap_exceeded: raise TaskError("command output exceeded bounded capture")
    if r.descendant_held_pipes: raise TaskError("command output stream did not close")
    return subprocess.CompletedProcess(argv,r.returncode or 0,r.stdout,r.stderr)

def _spawn_detail(argv:list[str])->str:
    """Why a process would not start, named. See the Claude lane's copy."""
    program=argv[0] if argv else "(no argv)"
    if not os.path.exists(program): reason="no such file"
    elif os.path.isdir(program): reason="is a directory"
    elif not os.access(program,os.X_OK): reason="not executable by this account"
    else: reason="the operating system refused to start it"
    return f"could not start {program!r}: {reason}"

def _env():
    e={"PATH":os.environ.get("PATH","/usr/bin:/bin"),"HOME":str(Path.home()),"LANG":"C.UTF-8",
       "GIT_CONFIG_NOSYSTEM":"1","GIT_CONFIG_GLOBAL":"/dev/null","GIT_CONFIG_SYSTEM":"/dev/null","GIT_TERMINAL_PROMPT":"0"}
    for k in ("USER","LOGNAME"):
        if os.environ.get(k): e[k]=os.environ[k]
    if os.name=="nt":
        # See claude_task._env for the measured cause: without SYSTEMROOT a
        # network-touching git operation fails with "Could not resolve host".
        for k in ("SYSTEMROOT","COMSPEC","PATHEXT","USERPROFILE"):
            if os.environ.get(k): e[k]=os.environ[k]
    return e

def _git(repo:Path,*args:str,timeout:int=30,env=None):
    r=_run(_git_argv(*args),cwd=repo,env=env or _env(),timeout=timeout)
    if r.returncode: raise TaskError("git prerequisite failed")
    return r.stdout.decode().strip()

def _git_argv(*args:str):
    return [str(_git_bin()),"--no-optional-locks","-c","core.hooksPath=/dev/null","-c","core.fsmonitor=false",
            "-c","diff.external=","-c","core.attributesFile=/dev/null",*args]

def _sha(path:Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

#: A task may legitimately start in a dirty tree. What must never happen is
#: that the task changes the user's working copy, and "git status text is
#: unchanged" does not say that: porcelain=v2 reports HEAD and index hashes
#: for a modified file, never the worktree content, and reports untracked
#: files by name only. Overwriting a file that was already modified, or
#: rewriting an untracked one, leaves the status output byte-identical. So the
#: snapshot hashes content as well.
MAX_SNAPSHOT_FILES=2048
MAX_SNAPSHOT_BYTES=256*1024*1024
MAX_SNAPSHOT_FILE_BYTES=64*1024*1024
SNAPSHOT_CHUNK_BYTES=1024*1024

def _relevant_paths(repo:Path,env)->list[str]:
    """Every path git considers not-clean, from a single NUL-delimited status."""
    raw=_git(repo,"status","--porcelain=v2","--untracked-files=all","-z",env=env)
    paths=[];fields=raw.split("\x00");i=0
    while i<len(fields):
        entry=fields[i];i+=1
        if not entry: continue
        kind=entry[0]
        if kind=="1":
            paths.append(entry.split(" ",8)[8])
        elif kind=="2":
            # A renamed entry is "<fields> <path>" followed by the original
            # path as its own NUL-delimited field. Both sides matter.
            paths.append(entry.split(" ",9)[9])
            if i<len(fields): paths.append(fields[i]);i+=1
        elif kind in ("?","!","u"):
            paths.append(entry.split(" ",10)[-1] if kind=="u" else entry[2:])
    return sorted(set(p for p in paths if p))

def _content_snapshot(repo:Path,env)->dict:
    """Bounded hash of the content of every not-clean file.

    Bounded, and a breach of the bound is a refusal rather than a smaller
    snapshot: a snapshot that silently skipped files would report integrity it
    never checked. The bound is deliberately far above any repository this
    lane is meant to run in.
    """
    paths=_relevant_paths(repo,env)
    if len(paths)>MAX_SNAPSHOT_FILES:
        raise TaskError("source snapshot exceeds the file bound; refusing to run without integrity coverage")
    digest=hashlib.sha256();total=0
    for rel in paths:
        target=repo/rel
        digest.update(rel.encode("utf-8","surrogateescape"));digest.update(b"\x00")
        try:
            info=target.lstat()
        except OSError:
            digest.update(b"absent\x00\x00");continue
        mode=info.st_mode
        if stat.S_ISLNK(mode):
            digest.update(b"link\x00");digest.update(os.readlink(target).encode("utf-8","surrogateescape"))
        elif stat.S_ISDIR(mode):
            # A submodule or an untracked directory git reported as one entry.
            digest.update(b"dir\x00")
        elif stat.S_ISREG(mode):
            total+=_hash_regular_file(target,digest,MAX_SNAPSHOT_BYTES-total)
        else:
            # A FIFO, socket or device node in the worktree. It is never
            # opened: reading one can block until something writes, or have
            # side effects on the host, and a snapshot that hangs is an
            # integrity check that never completes.
            raise TaskError("source snapshot found a file that is not regular, a directory or a symlink")
        digest.update(b"\x00")
    return {"files":len(paths),"bytes":total,"sha256":digest.hexdigest()}

def _hash_regular_file(target:Path,digest,budget:int)->int:
    """Hash one regular file through a descriptor this function opened.

    Opened with O_NOFOLLOW, so a symlink swapped in between the enumeration
    and the read is refused rather than followed, and with O_NONBLOCK, so a
    node that is not what lstat just said it was cannot block the open. The
    type and size are then re-checked on the descriptor itself, because the
    name was checked a moment ago and the descriptor is what is actually read.

    Bytes are counted as they arrive and charged against both the per-file and
    the cumulative bound, and the identity is re-checked at the end. A file
    that grows or is replaced mid-read is a refusal, never a hash of a prefix
    that would compare equal to the one taken before the task.
    """
    # O_BINARY matters as much as the other two on Windows: without it,
    # os.open() defaults to text mode, os.read() then translates "\r\n" to
    # "\n" in whatever it returns, and the bytes read fall short of fstat's
    # raw st_size for any file that has one -- indistinguishable from this
    # function's own "file changed size mid-read" refusal (measured: a
    # 14-byte CRLF file reads back as 12 bytes without this flag).
    flags=os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)|getattr(os,"O_NONBLOCK",0)|getattr(os,"O_BINARY",0)
    try: descriptor=os.open(target,flags)
    except OSError as exc: raise TaskError("source snapshot could not read a changed file") from exc
    try:
        opened=os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise TaskError("source snapshot found a non-regular file where a regular file was expected")
        if opened.st_size>MAX_SNAPSHOT_FILE_BYTES:
            raise TaskError("source snapshot exceeds the per-file byte bound; refusing to run without integrity coverage")
        if opened.st_size>budget:
            raise TaskError("source snapshot exceeds the byte bound; refusing to run without integrity coverage")
        if hasattr(os,"O_NONBLOCK"):
            # Only undoing what was only ever set on a platform that has it:
            # Windows never opened with O_NONBLOCK (the flag above is 0
            # there), and os.set_blocking() on a Windows file descriptor
            # raises OSError (WinError 87) rather than being a no-op.
            os.set_blocking(descriptor,True)
        digest.update(b"file\x00");digest.update(str(opened.st_mode&0o777).encode("ascii"));digest.update(b"\x00")
        read=0
        while True:
            chunk=os.read(descriptor,SNAPSHOT_CHUNK_BYTES)
            if not chunk: break
            read+=len(chunk)
            if read>opened.st_size or read>budget:
                raise TaskError("a file grew while the source snapshot was being taken")
            digest.update(chunk)
        if read!=opened.st_size:
            raise TaskError("a file changed size while the source snapshot was being taken")
        final=os.fstat(descriptor)
        if (final.st_dev,final.st_ino,final.st_size)!=(opened.st_dev,opened.st_ino,opened.st_size):
            raise TaskError("a file was replaced while the source snapshot was being taken")
        digest.update(str(read).encode("ascii"))
        return read
    finally:
        os.close(descriptor)

def _source_state(repo:Path,env):
    return {"head":_git(repo,"rev-parse","HEAD",env=env),
            "status":_git(repo,"status","--porcelain=v2","--untracked-files=all",env=env),
            "config_sha256":_sha(repo/".git"/"config"),
            "content":_content_snapshot(repo,env)}

def _require_confinement(classification:str)->hostenv.Confinement:
    """The verification confinement this host will apply, or a named refusal.

    Only verification needs it: generation runs inside Codex's own
    ``workspace-write`` sandbox and the patch steps are git. Unlike the
    Claude lane, Codex verification commands are optional, so a host with no
    backend still refuses here rather than running a job whose verification
    would be silently skipped."""
    try: return hostenv.confinement(classification)
    except hostenv.HostCapabilityError as exc: raise TaskError(str(exc)) from None

def _atomic_json(path:Path,value:dict):
    fd,tmp=tempfile.mkstemp(prefix=".receipt-",dir=path.parent)
    try:
        # os.fchmod does not exist on Windows at all (AttributeError, measured
        # on a live VM). agent_platform picks the real primitive per host:
        # fchmod on POSIX, an owner-only ACL on Windows.
        try: agent_platform.enforce_owner_only_file(fd)
        except BaseException:
            try: os.close(fd)
            except OSError: pass
            raise
        with os.fdopen(fd,"w") as f: json.dump(value,f,indent=2,sort_keys=True); f.write("\n"); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass

def _verify_argv(commands:list[list[str]]):
    if not commands: return []
    try: return verify_policy.check_verify_argv(commands)
    except verify_policy.VerifyPolicyError as exc: raise TaskError(str(exc)) from None

def _auth(codex_bin:Path,codex_home:Path,env):
    e={**env,"CODEX_HOME":str(codex_home)}
    r=_run([str(codex_bin),"login","status"],cwd=codex_home,env=e,timeout=30)
    text=(r.stdout+r.stderr).decode("utf-8","replace").lower()
    if "not logged in" in text or "no credentials" in text:
        raise TaskError("Codex is not authenticated in the isolated CODEX_HOME")
    if r.returncode==0 and "logged in using chatgpt" in text:
        return {"auth_method":"chatgpt"}
    raise TaskError("Codex is not authenticated through a supported ChatGPT subscription")

def _command(codex_bin:Path,model:str|None,reasoning_effort:str|None,workspace:Path,last_message_file:Path):
    argv=[str(codex_bin),"exec","--json","--ignore-user-config","--ignore-rules",
          "--strict-config","--skip-git-repo-check",
          "--output-last-message",str(last_message_file),
          "-c",'sandbox_mode="workspace-write"',
          "-c",'sandbox_workspace_write.network_access=false']
    if reasoning_effort:
        argv+=["-c",f'model_reasoning_effort="{reasoning_effort}"']
    argv+=["-s","workspace-write","-C",str(workspace)]
    if model and model!="default":
        argv+=["-m",model]
    return argv

def _parse_events(stdout:bytes):
    thread_id=None; errors=[]; events=[]
    for line in stdout.decode("utf-8","replace").splitlines():
        line=line.strip()
        if not line or not line.startswith("{"): continue
        try: event=json.loads(line)
        except ValueError: continue
        if not isinstance(event,dict): continue
        events.append(event)
        etype=event.get("type")
        if etype=="thread.started":
            candidate=event.get("thread_id")
            if isinstance(candidate,str) and candidate and not thread_id:
                thread_id=candidate
        elif etype in ("error","turn.failed"):
            failure=event.get("error")
            message=(failure.get("message") if etype=="turn.failed" and isinstance(failure,dict)
                     else event.get("message"))
            if isinstance(message,str): errors.append(message)
    return thread_id,errors,events

def _patch(tree:Path,base:str,env):
    ignored=_git(tree,"ls-files","--others","--ignored","--exclude-standard",env=env)
    if ignored: raise TaskError("Codex produced ignored untracked files")
    add=_run(_git_argv("add","-N","--","."),cwd=tree,env=env,timeout=30)
    if add.returncode: raise TaskError("could not stage intent-to-add entries")
    r=_run(_git_argv("diff","--binary","--no-ext-diff","--no-textconv",base),cwd=tree,env=env,timeout=60)
    if r.returncode: raise TaskError("could not capture patch")
    if not r.stdout: raise TaskError("Codex produced an empty patch")
    return r.stdout

def _remove(repo:Path,tree:Path,env):
    if not tree.exists(): return True
    try:
        result=_run(_git_argv("worktree","remove","--force",str(tree)),cwd=repo,env=env,timeout=60)
    except (OSError,TaskError,subprocess.SubprocessError):
        return False
    # Never run repository-wide `worktree prune`: it can discard unrelated
    # users' stale-but-recoverable worktree registrations.
    return result.returncode==0 and not tree.exists()

def _sandboxed(command:list[str],tree:Path,scratch:Path,env:dict[str,str],timeout:int,
               backend:hostenv.Confinement|None=None):
    """Run one verification command under this host's confinement backend."""
    backend=backend or _require_confinement("synthetic")
    if backend.name==hostenv.LINUX_USERNS:
        return _netns_confined(command,tree,scratch,env,timeout,backend)
    return _sandbox_exec_confined(command,tree,scratch,env,timeout,backend)

def _netns_confined(command:list[str],tree:Path,scratch:Path,env:dict[str,str],timeout:int,
                    backend:hostenv.Confinement):
    """Network denied and writes confined by a mount namespace. Reads are not.

    See the Claude lane for the full note. The boundary re-proves itself for this worktree on every command: the
    helper refuses to exec anything until a write to each canary path has
    actually failed. A failed boundary is a refusal with its own name, never a
    verification result, because a command that did not run under the
    confinement it claims has told us nothing."""
    scratch.mkdir(mode=0o700)
    sandbox_env={**env,"HOME":str(scratch),"TMPDIR":str(scratch),"TMP":str(scratch),"TEMP":str(scratch)}
    sandbox_env.pop("CODEX_HOME",None)
    if command[0]=="git": command=_git_argv(*command[1:])
    started=time.monotonic()
    result=_run(hostenv.confined_argv(command,tree=tree,scratch=scratch,
                                      canaries=_confinement_canaries(tree)),
                cwd=tree,env=sandbox_env,timeout=timeout)
    if result.returncode==hostenv.CONFINEMENT_SELFTEST_EXIT:
        raise TaskError("verification confinement failed its own canary check on this host "
                        "[confinement_selftest_failed]")
    result.sandbox_backend=backend.name
    result.sandbox_profile_sha256=None
    result.duration_seconds=time.monotonic()-started
    return result

def _confinement_canaries(tree:Path)->list[str]:
    """Paths a verification command must not be able to write.

    The source repository is first because a write there is the exact damage
    the old post-run snapshot was supposed to catch and could only report
    after the fact. The job directory is next: the scratch directory inside it
    is deliberately writable, the directory itself is not. Then the two places
    any escape would naturally aim for."""
    canaries=[str(tree.parent)]
    marker=tree/".git"
    try:
        text=marker.read_text().strip() if marker.is_file() else ""
    except OSError:
        text=""
    if text.startswith("gitdir: "):
        gitdir=Path(text[8:])
        try:
            commondir=(gitdir/(gitdir/"commondir").read_text().strip()).resolve()
        except OSError:
            commondir=None
        if commondir is not None and commondir.parent.is_dir():
            canaries.append(str(commondir.parent))
    for extra in (str(Path.home()),tempfile.gettempdir()):
        if extra not in canaries and os.path.isdir(extra):
            canaries.append(extra)
    return canaries

def _sandbox_exec_confined(command:list[str],tree:Path,scratch:Path,env:dict[str,str],timeout:int,
                           backend:hostenv.Confinement):
    scratch.mkdir(mode=0o700)
    profile=scratch/"verify.sb"
    def quoted(value:Path): return str(value).replace('\\','\\\\').replace('"','\\"')
    executable=Path(_git_bin() if command[0]=="git" else (shutil.which(command[0],path=env.get("PATH")) or command[0])).resolve()
    runtime_root=executable.parent.parent if str(executable).startswith("/Users/") else executable.parent
    read_roots=[Path("/System"),Path("/usr"),Path("/bin"),Path("/sbin"),Path("/Library/Frameworks"),Path("/Library/Developer"),
                Path("/etc"),Path("/var/db"),Path("/var/select"),Path("/var/run"),Path("/private/etc"),
                Path("/private/var/db"),Path("/private/var/select"),Path("/private/var/run"),
                tree,tree.resolve(),scratch,scratch.resolve(),runtime_root]
    # A linked worktree's index and object database live under the source
    # repository's git directory. Permit only those git internals, never the
    # source working tree or its local config.
    git_marker=tree/".git"
    marker=git_marker.read_text().strip() if git_marker.exists() else ""
    if marker.startswith("gitdir: "):
        gitdir=Path(marker[8:]).resolve()
        commondir=(gitdir/(gitdir/"commondir").read_text().strip()).resolve()
        read_roots.extend([gitdir,commondir/"objects",commondir/"refs",commondir/"HEAD",commondir/"packed-refs",commondir/"config"])
    read_rules=" ".join(f'(subpath "{quoted(path)}")' for path in read_roots)
    ancestors=set()
    for path in read_roots:
        ancestors.update(path.resolve().parents)
    ancestor_rules=" ".join(f'(literal "{quoted(path)}")' for path in ancestors)
    profile.write_text('(version 1)\n(allow default)\n(deny network*)\n(deny file-read-data)\n(deny file-write*)\n'
                       f'(allow file-read-data {read_rules} {ancestor_rules} (literal "/dev/null") (literal "/dev/urandom"))\n'
                       f'(allow file-write* (literal "/dev/null") (subpath "{quoted(tree)}") (subpath "{quoted(tree.resolve())}") '
                       f'(subpath "{quoted(scratch)}") (subpath "{quoted(scratch.resolve())}"))\n')
    os.chmod(profile,0o600)
    sandbox_env={**env,"HOME":str(scratch),"TMPDIR":str(scratch),"TMP":str(scratch),"TEMP":str(scratch)}
    # The base environment never carried CODEX_HOME (it is added per Codex
    # call), and this makes that invariant explicit rather than inherited:
    # verification runs project code and has no use for a credential store.
    sandbox_env.pop("CODEX_HOME",None)
    if command[0]=="git": command=_git_argv(*command[1:])
    started=time.monotonic()
    result=_run([hostenv.SANDBOX_EXEC,"-f",str(profile),*command],cwd=tree,env=sandbox_env,timeout=timeout)
    result.sandbox_backend=backend.name
    result.sandbox_profile_sha256=_sha(profile)
    result.duration_seconds=time.monotonic()-started
    return result

def _assert_no_ancestor_contamination(job:Path):
    """Refuse if anything above this disposable job directory is contaminated.

    Deliberately does not inspect the worktree itself: that is the caller's
    own repository, already access-controlled by classification, and Codex is
    entitled to read its own checked-out AGENTS.md/CLAUDE.md. What must never
    happen is Codex walking further up into the bridge's own directories
    (task root, home, filesystem root) and picking up an unrelated
    instruction file nobody meant to expose to this job.
    """
    try:
        preflight.assert_workspace_clean(str(job))
    except BrokerError as exc:
        raise TaskError(f"ancestor instruction-file check failed: {exc.category.value}") from exc

def run_task(*,codex_bin:Path,promotion_dir:Path|None=None,**kwargs):
    """Run one task under codex-bridge's CLI promotion admission.

    Admission comes before everything else, including request validation,
    because it has to cover every Codex process this task starts, starting with
    ``codex --version``: the shared lock is held until the task returns. See
    codex_promotion.py. The task then executes the path admission verified,
    never the launcher name.
    """
    node_bin=shutil.which("node",path=_env()["PATH"])
    try:
        with codex_promotion.admit(codex_bin,promotion_dir=promotion_dir,node_bin=node_bin) as admission:
            return _run_admitted(codex_bin=admission.exec_path,admission=admission,**kwargs)
    except codex_promotion.AdmissionRefused as exc:
        raise TaskError(f"Codex CLI admission refused: {exc}") from exc

def _run_admitted(*,brief:Path,repo:Path,task_root:Path,codex_bin:Path,admission:codex_promotion.Admission,
                  codex_home:Path=DEFAULT_CODEX_HOME,
                  classification:str,model:str|None=None,reasoning_effort:str|None=None,
                  verify_argv:list[list[str]]|None=None,base:str="HEAD",timeout:int=900,verify_timeout:int=300):
    if classification not in ALLOWED_CLASSIFICATIONS: raise TaskError("execution lane refuses client-derived material")
    if any(not p.is_absolute() for p in (brief,repo,task_root,codex_bin,codex_home)): raise TaskError("all paths must be absolute")
    if not repo.is_dir() or not (repo/".git").is_dir(): raise TaskError("repo must be a primary git checkout")
    if not codex_bin.is_file() or not os.access(codex_bin,os.X_OK): raise TaskError("Codex executable unavailable")
    raw=brief.read_bytes()
    if not raw or len(raw)>MAX_BRIEF_BYTES: raise TaskError("brief empty or too large")
    try: brief_text=raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc: raise TaskError("brief must be UTF-8") from exc
    checks=_verify_argv(verify_argv or []); env=_env(); source_before=_source_state(repo,env)
    # The host is probed only after the REQUEST has been validated. Ordering
    # matters for the message an operator sees: with the probe first, a
    # refused verification command on a host with no confinement backend
    # reported the host refusal and buried the real mistake, which broke a
    # pre-existing test and would have misdirected anybody reading the
    # receipt. Validate what was asked, then interrogate the machine.
    backend=_require_confinement(classification)
    base_sha=_git(repo,"rev-parse","--verify",f"{base}^{{commit}}",env=env)
    version=_run([str(codex_bin),"--version"],cwd=codex_bin.parent,env=env,timeout=30)
    if version.returncode: raise TaskError("could not identify Codex executable")
    admission.check_reported_version(version.stdout.decode("utf-8","replace"))
    task_root.mkdir(mode=0o700,parents=True,exist_ok=True); os.chmod(task_root,0o700)
    # mkdir(mode=) and chmod are no-ops on Windows; without this, task_root and
    # every job directory under it inherit whatever their parent grants there.
    try: wpv.require_private_directory(task_root,root=task_root)
    except wpv.PrivacyError as exc: raise TaskError(f"execution task root could not be protected [{exc.reason}]") from exc
    job=task_root/uuid.uuid4().hex; job.mkdir(mode=0o700); gen=job/"generation-worktree"; fresh=job/"verification-worktree"
    try: wpv.require_private_directory(job,root=task_root)
    except wpv.PrivacyError as exc: raise TaskError(f"execution job directory could not be protected [{exc.reason}]") from exc
    receipt={"schema":2,"job_id":job.name,"status":"running","route":"codex-subscription-cli","classification":classification,
             "base_sha":base_sha,"brief_sha256":hashlib.sha256(raw).hexdigest(),"model_requested":model,
             "reasoning_effort_requested":reasoning_effort,
             "permission_to_land":False,"started_at":time.time(),"executable_realpath":str(codex_bin.resolve()),
             "executable_sha256":_sha(codex_bin.resolve()),"executable_version":version.stdout.decode("utf-8","replace").strip(),
             "codex_home":str(codex_home),"host_platform":platform.system(),"cli_admission":admission.receipt(),
             "git_executable":str(_git_bin()),"verification_confinement":backend.name,
             "confinement_denies_network":backend.denies_network,
             "confinement_confines_reads":backend.confines_reads,
             "confinement_confines_writes":backend.confines_writes,
             "source_before":source_before}; _atomic_json(job/"receipt.json",receipt)
    pending_exc=None
    try:
        _assert_no_ancestor_contamination(job)
        store.secure_mkdir(str(codex_home))
        try:
            preflight.assert_peer_home_has_no_config(str(codex_home))
        except BrokerError as exc:
            raise TaskError(f"isolated CODEX_HOME carries unexpected config: {exc.category.value}") from exc
        receipt["auth"]=_auth(codex_bin,codex_home,env)
        receipt["codex_home_inventory"]=preflight.peer_home_inventory(str(codex_home))
        _git(repo,"worktree","add","--detach",str(gen),base_sha,timeout=120,env=env); marker=(gen/".git").read_bytes()
        last_message_file=job/"codex-last-message.txt"
        argv=_command(codex_bin,model,reasoning_effort,gen,last_message_file)
        codex_env={**env,"CODEX_HOME":str(codex_home)}
        r=_run(argv,cwd=gen,env=codex_env,timeout=timeout,input_bytes=("TASK BRIEF\n\n"+brief_text).encode())
        for n,data in (("codex.stdout",r.stdout),("codex.stderr",r.stderr)):
            (job/n).write_bytes(data); os.chmod(job/n,0o600)
            receipt[n.replace(".","_")+"_sha256"]=hashlib.sha256(data).hexdigest()
        thread_id,error_messages,events=_parse_events(r.stdout)
        failed=r.returncode not in (0,None) or any(e.get("type")=="turn.failed" for e in events)
        receipt["response_metadata"]={"thread_id":thread_id,"event_count":len(events),
                                      "event_types":sorted({str(e.get("type")) for e in events})[:20],
                                      "error_event_count":len(error_messages)}
        if failed: raise TaskError(_with_error_detail(f"Codex exited with status {r.returncode}",error_messages))
        if not thread_id: raise TaskError("Codex did not report a thread id")
        last_message=last_message_file.read_bytes() if last_message_file.is_file() else b""
        if not last_message.strip(): raise TaskError("Codex produced no final message")
        receipt["last_message_sha256"]=hashlib.sha256(last_message).hexdigest()
        if (gen/".git").read_bytes()!=marker: raise TaskError("Codex altered git metadata")
        patch=_patch(gen,base_sha,env); pp=job/"changes.patch"; pp.write_bytes(patch); os.chmod(pp,0o600); _remove(repo,gen,env)
        _git(repo,"worktree","add","--detach",str(fresh),base_sha,timeout=120,env=env)
        a=_run(_git_argv("apply","--binary","--whitespace=nowarn",str(pp)),cwd=fresh,env=env,timeout=60)
        if a.returncode: raise TaskError("exact patch did not apply to fresh worktree")
        applied=_patch(fresh,base_sha,env)
        if hashlib.sha256(applied).digest()!=hashlib.sha256(patch).digest(): raise TaskError("fresh patch differs from generated patch")
        evidence=[]
        for i,c in enumerate(checks,1):
            scratch=job/f"verify-{i}-scratch"
            v=_sandboxed(c,fresh,scratch,env,verify_timeout,backend)
            outlog=job/f"verify-{i}.stdout"; errlog=job/f"verify-{i}.stderr"
            outlog.write_bytes(v.stdout); errlog.write_bytes(v.stderr); os.chmod(outlog,0o600); os.chmod(errlog,0o600)
            evidence.append({"argv":c,"returncode":v.returncode,"sandbox":v.sandbox_backend,
                             "sandbox_profile_sha256":v.sandbox_profile_sha256,"duration_seconds":v.duration_seconds,
                             "stdout_sha256":hashlib.sha256(v.stdout).hexdigest(),"stderr_sha256":hashlib.sha256(v.stderr).hexdigest()})
        # The delivered patch is a file path, and verification ran between
        # writing it and returning it. Confinement is what stops a verify
        # command reaching the job directory, and this is the check that does
        # not depend on confinement being correct: re-read the bytes and
        # compare them to what was generated. Without it the receipt could
        # record one digest while the caller applied different bytes.
        if hashlib.sha256(pp.read_bytes()).digest()!=hashlib.sha256(patch).digest():
            raise TaskError("delivered patch changed during verification")
        receipt.update(status="verification_passed_pending_integrity" if all(x["returncode"]==0 for x in evidence) else "verification_failed_pending_integrity",
                       generated_patch_sha256=hashlib.sha256(patch).hexdigest(),applied_patch_sha256=hashlib.sha256(applied).hexdigest(),
                       patch_sha256=hashlib.sha256(patch).hexdigest(),patch_bytes=len(patch),verification=evidence,
                       fresh_worktree_patch_match=True,finished_at=time.time())
    except Exception as exc:
        pending_exc=exc
        # TaskError messages are fixed, operator-safe diagnostics defined by
        # this harness. Persist them in the mode-0600 receipt so failures can
        # be corrected without exposing provider output or credentials.
        detail=str(exc) if isinstance(exc,TaskError) else None
        receipt.update(status="failed_pending_integrity",error=type(exc).__name__,
                       error_detail=detail,finished_at=time.time())
    finally:
        generation_removed=_remove(repo,gen,env)
        verification_removed=_remove(repo,fresh,env)
        cleanup_ok=generation_removed and verification_removed
        receipt["cleanup"]={"generation_worktree":str(gen),"generation_removed":generation_removed,
                            "verification_worktree":str(fresh),"verification_removed":verification_removed}
    try:
        source_after=_source_state(repo,env)
        source_match=source_after==source_before
        receipt["source_after"]=source_after
    except Exception:
        source_match=False
    receipt["source_integrity_match"]=source_match
    if not cleanup_ok:
        receipt.update(status="failed",error="WorktreeCleanupError",finished_at=time.time())
    elif not source_match:
        receipt.update(status="failed",error="SourceIntegrityError",finished_at=time.time())
    elif pending_exc is not None:
        receipt.update(status="failed",error=type(pending_exc).__name__,finished_at=time.time())
    elif receipt["status"]=="verification_passed_pending_integrity":
        receipt["status"]="complete"
    elif receipt["status"]=="verification_failed_pending_integrity":
        receipt["status"]="verification_failed"
    _atomic_json(job/"receipt.json",receipt)
    if not cleanup_ok: raise TaskError("disposable worktree cleanup failed")
    if not source_match: raise TaskError("source repository integrity changed during task")
    if pending_exc is not None: raise pending_exc
    return {**receipt,"job_dir":str(job),"patch":str(job/"changes.patch")}

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("brief",type=Path); p.add_argument("--repo",required=True,type=Path)
    p.add_argument("--codex-bin",type=Path,default=Path(shutil.which("codex") or "codex"),dest="codex_bin")
    p.add_argument("--codex-home",type=Path,default=DEFAULT_CODEX_HOME,dest="codex_home")
    p.add_argument("--classification",required=True,choices=sorted(ALLOWED_CLASSIFICATIONS))
    p.add_argument("--model",default=None); p.add_argument("--reasoning-effort",default=None,dest="reasoning_effort")
    p.add_argument("--base",default="HEAD"); p.add_argument("--timeout",type=int,default=900)
    p.add_argument("--verify-timeout",type=int,default=300)
    p.add_argument("--tasks-dir",type=Path,default=DEFAULT_TASK_ROOT,dest="task_root")
    p.add_argument("--verify-json",action="append",default=[])
    a=p.parse_args(argv)
    try:
        checks=[json.loads(x) for x in a.verify_json]; del a.verify_json; result=run_task(**vars(a),verify_argv=checks)
    except (OSError,ValueError,TaskError,subprocess.SubprocessError) as exc:
        # Same contract as claude_task: the class name alone told an operator
        # nothing (queue jobs failed for a day as a bare "TaskError"). TaskError
        # text is fixed, operator-safe diagnostics defined by this harness.
        failure={"ok":False,"error":type(exc).__name__}
        if isinstance(exc,TaskError): failure["error_detail"]=str(exc)
        print(json.dumps(failure,sort_keys=True)); return 1
    # A completed process is not a successful task. run_task returns normally
    # when the generated patch applied cleanly but the verification commands
    # failed, and reporting ok:true with exit 0 for that told every caller,
    # including the execution queue, that unverified work had passed.
    ok=result.get("status")=="complete"
    print(json.dumps({"ok":ok,**result},sort_keys=True)); return 0 if ok else 3

if __name__=="__main__": raise SystemExit(main())

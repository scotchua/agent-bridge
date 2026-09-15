"""Temporary CI diagnostic; see .github/workflows/win-probe.yml."""
import csv, os, subprocess, sys, tempfile

def run(argv, **kw):
    r = subprocess.run(argv, capture_output=True, text=True, **kw)
    return r.returncode, (r.stdout + r.stderr).strip()

print("python", sys.version.split()[0], "shell-env MSYSTEM=", os.environ.get("MSYSTEM"))
for k in ("TMPDIR", "TEMP", "TMP", "RUNNER_TEMP", "HOME", "USERPROFILE", "PATH"):
    print(k, "=", os.environ.get(k))
print("gettempdir =", tempfile.gettempdir(), "realpath", os.path.realpath(tempfile.gettempdir()))
print("where whoami:", run("where whoami", shell=True)[1])
print("where icacls:", run("where icacls", shell=True)[1])
print("whoami /priv:\n", run(["whoami", "/priv"])[1])
rc, out = run(["whoami", "/user", "/fo", "csv", "/nh"])
print("whoami /user:", rc, out)
sid = next(csv.reader([out.strip()]))[1]
base = tempfile.mkdtemp(prefix="probe-")
os.makedirs(os.path.join(base, "ws"))
drive = os.path.splitdrive(os.path.realpath(base))[0]
print("volume:", run(["fsutil", "fsinfo", "volumeinfo", drive + "\\"])[1])
print("deny RD:", run(["icacls", base, "/deny", f"*{sid}:(RD)"]))
print("listing:", run(["icacls", base])[1])
try:
    print("listdir SUCCEEDED (deny ineffective):", os.listdir(base))
except OSError as exc:
    print("listdir refused (deny effective):", exc)
print("remove:", run(["icacls", base, "/remove:d", f"*{sid}"])[0])
print("deny WD,AD:", run(["icacls", base, "/deny", f"*{sid}:(WD,AD)"]))
try:
    os.makedirs(os.path.join(base, "nested"))
    print("mkdir SUCCEEDED (deny ineffective)")
except OSError as exc:
    print("mkdir refused (deny effective):", exc)
print("remove:", run(["icacls", base, "/remove:d", f"*{sid}"])[0])

"""Real packaged-app smoke test for the folder buttons (Windows only).

Runs dist/TriAgentStudio/TriAgentStudio.exe --selftest-folders (real desktop session), which
clicks the real dashboard buttons. Independently verifies via the Windows Shell COM API that
new Explorer windows opened at exactly the expected folders, then closes only those windows.
A second launch verifies the project is restored (not recreated). Uses an isolated temporary
data folder and projects folder - the user's real projects are not touched.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "dist" / "TriAgentStudio" / "TriAgentStudio.exe"

PS_LIST = ("$s = New-Object -ComObject Shell.Application; "
           "$s.Windows() | ForEach-Object { try { $_.LocationURL } catch {} }")
PS_CLOSE = ("$t = @({targets}); $s = New-Object -ComObject Shell.Application; "
            "$s.Windows() | ForEach-Object {{ if ($t -contains $_.LocationURL) {{ $_.Quit() }} }}")


def explorer_paths() -> dict[str, str]:
    out = subprocess.run(["powershell", "-NoProfile", "-Command", PS_LIST], capture_output=True,
                         text=True, timeout=60).stdout
    res = {}
    for url in (l.strip() for l in out.splitlines()):
        if url.startswith("file:"):
            p = unquote(urlparse(url).path).lstrip("/").replace("/", "\\")
            res[url] = str(Path(p).resolve()).lower()
    return res


def run_selftest(env, report: Path, only: str | None = None) -> dict:
    cmd = [str(EXE), "--selftest-folders", str(report)] + (["--only", only] if only else [])
    subprocess.run(cmd, env=env, timeout=120)
    return json.loads(report.read_text(encoding="utf-8"))


def close_windows(urls):
    targets = ",".join("'" + u.replace("'", "''") + "'" for u in urls)
    if targets:
        subprocess.run(["powershell", "-NoProfile", "-Command", PS_CLOSE.format(targets=targets)],
                       timeout=60)


def main() -> int:
    if not EXE.exists():
        print("Executable not built:", EXE)
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="triagent-pkg-"))
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)                       # real desktop session
    env["TRIAGENT_HOME"] = str(tmp / "home")
    env["TRIAGENT_PROJECTS_DIR"] = str(tmp / "Documents" / "TriAgent Studio Projects")
    # one launch per button, verifying each Explorer window before the next click
    r1, opened_windows, checks = None, [], {}
    for k, name in enumerate(("Open Project Folder", "Open Output Folder",
                              "Open Application Folder")):
        before = explorer_paths()
        rep = run_selftest(env, tmp / f"report_{k}.json", only=name)
        r1 = r1 or rep
        time.sleep(4)
        new = {u: p for u, p in explorer_paths().items() if u not in before}
        expected = rep["expected"][name]
        opened = rep["opened"][name]
        checks[f"{name}: shell accepted"] = bool(opened) and all(ok for _, ok in opened)
        checks[f"{name}: requested path correct"] = bool(opened) and             Path(opened[0][0]).resolve() == Path(expected).resolve()
        checks[f"{name}: Explorer window open at path"] =             str(Path(expected).resolve()).lower() in new.values()
        opened_windows += list(new.values())
        close_windows(new)                       # close only what this test opened
    checks["packaged_exe_frozen"] = r1["frozen"]
    checks["first_launch_created_projects_folder"] = Path(r1["projects_root"]).is_dir()
    checks["project_created_in_projects_folder"] = Path(
        r1["expected"]["Open Project Folder"]).parent == Path(r1["projects_root"])
    checks["first launch created the project"] = r1["project_created"]
    checks["Application folder == folder of TriAgentStudio.exe"] = \
        Path(r1["expected"]["Open Application Folder"]) == EXE.parent.resolve()
    # second launch: the same project must be restored, not recreated
    before = explorer_paths()
    r2 = run_selftest(env, tmp / "report_restart.json", only="Open Project Folder")
    time.sleep(3)
    close_windows([u for u in explorer_paths() if u not in before])
    checks["restart: project restored (not recreated)"] = (not r2["project_created"] and
                                                           r2["project"] == r1["project"])
    checks["restart: same project folder"] = r2["expected"]["Open Project Folder"] == \
        r1["expected"]["Open Project Folder"]
    result = {"checks": checks, "first_launch": r1, "second_launch": r2,
              "explorer_windows_opened": opened_windows}
    out = ROOT / "docs" / "packaged_smoke_result.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    ok = all(checks.values())
    print(f"\nOverall: {'SUCCESS' if ok else 'FAILURES'}  -> {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

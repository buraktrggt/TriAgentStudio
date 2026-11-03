"""Project management & folder access: default folder, restore, new/switch, open folders,
invalid-workspace recovery, migration, persistence and packaged path resolution."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sqlite3
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox

from triagent import paths
from triagent.core.projects import ProjectRegistry
from triagent.core.safety import workspace_issue
from triagent.ui import desktop
from triagent.ui.controller import AppController
from triagent.ui.main_window import MainWindow

from .conftest import wait_for


@pytest.fixture
def opened(monkeypatch):
    """Record native open requests instead of launching Explorer during unit tests."""
    urls = []
    monkeypatch.setattr(QDesktopServices, "openUrl",
                        staticmethod(lambda url: urls.append(url.toLocalFile()) or True))
    return urls


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: a[4] if len(a) > 4 else QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)


def _registry(tmp_path):
    reg = ProjectRegistry(tmp_path / "home")
    reg.set("setup_done", "1")
    return reg


def _window(qtbot, reg):
    win = MainWindow(AppController(reg))
    qtbot.addWidget(win)
    win.show()
    win.bootstrap()
    return win


# ------------------------------------------------------------------ default folder
def test_first_launch_creates_default_projects_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIAGENT_PROJECTS_DIR")
    docs = tmp_path / "MyDocs"
    docs.mkdir()
    monkeypatch.setattr(paths, "documents_dir", lambda: docs)
    reg = _registry(tmp_path)
    assert not (docs / "TriAgent Studio Projects").exists()
    ctl = AppController(reg)
    root = docs / "TriAgent Studio Projects"
    assert root.is_dir() and ctl.projects_root == root.resolve()
    assert reg.get("projects_dir") == str(root.resolve())          # persisted in app data


def test_documents_resolved_with_qstandardpaths():
    from PySide6.QtCore import QStandardPaths
    qt = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
    assert str(paths.documents_dir()) == str(Path(qt))


# ------------------------------------------------------------------ new project
def test_new_project_creates_named_folder_and_selects_it(tmp_path):
    reg = _registry(tmp_path)
    ctl = AppController(reg)
    p = ctl.new_project("Calculator App")
    folder = ctl.projects_root / "Calculator App"
    assert folder.is_dir() and Path(p["workspace"]) == folder.resolve()
    assert ctl.project["id"] == p["id"] and reg.get("last_project") == p["id"]
    (folder / "x.txt").write_text("keep")
    p2 = ctl.new_project("Calculator App")                       # never reuses a full folder
    assert Path(p2["workspace"]).name == "Calculator App (2)"
    assert (folder / "x.txt").read_text() == "keep"
    p3 = ctl.new_project('Bad:/Name*?')
    assert Path(p3["workspace"]).parent == ctl.projects_root and Path(p3["workspace"]).is_dir()


def test_new_project_refuses_application_folder(tmp_path):
    ctl = AppController(_registry(tmp_path))
    with pytest.raises(ValueError):
        ctl.new_project("x", str(paths.app_install_dir() / "dist" / "TriAgentStudio"))


# ------------------------------------------------------------------ restore / persistence
def test_last_project_restored_without_starting_agents(qtbot, tmp_path, quiet):
    reg = _registry(tmp_path)
    ctl = AppController(reg)
    a = ctl.new_project("Alpha")
    ctl.repo.save_prompt("shared", "ALPHA SHARED", 0)
    ctl.repo.save_prompt("codex", "ALPHA CODEX", 0)
    ctl.settings["providers"]["claude"]["count"] = 7
    ctl.save_settings()
    run = ctl.repo.create_run("old objective", {"settings": {}, "available": []})
    ctl.repo.set_run_status(run, "completed", ended=True)
    b = ctl.new_project("Beta")
    ctl.open_project(a["id"])                                      # last opened = Alpha
    ctl.shutdown()
    win = _window(qtbot, reg)                                      # "restart"
    c = win.ctl
    assert c.project["id"] == a["id"]
    assert c.prompts()["shared"] == "ALPHA SHARED" and c.prompts()["codex"] == "ALPHA CODEX"
    assert c.settings["providers"]["claude"]["count"] == 7
    assert c.view_run_id == run                                    # history restored
    assert win.dashboard.proj_name.text() == "Alpha"
    assert win.dashboard.proj_path.text() == a["workspace"]
    assert not any(e.running for e in c.engines.values())          # nothing auto-started
    assert c.repo.store.q1("SELECT COUNT(*) FROM worker_procs")[0] == 0
    assert c.repo.get_run(run)["status"] == "completed"


def test_switch_project_and_recent_dropdown(qtbot, tmp_path, quiet):
    reg = _registry(tmp_path)
    ctl = AppController(reg)
    a = ctl.new_project("Alpha")
    b = ctl.new_project("Beta")
    ctl.shutdown()
    win = _window(qtbot, reg)
    assert win.ctl.project["id"] == b["id"]
    win.dashboard.refresh_project_card()
    names = [win.dashboard.recent.itemText(i) for i in range(win.dashboard.recent.count())]
    assert "Alpha" in names and "Beta" in names
    win.switch_to(a["id"])
    assert win.ctl.project["id"] == a["id"] and reg.get("last_project") == a["id"]
    from triagent.ui.project_dialogs import SwitchProjectDialog
    dlg = SwitchProjectDialog(win.ctl.projects(), a["id"])
    qtbot.addWidget(dlg)
    assert dlg.table.rowCount() == 2 and dlg.selected_id() == a["id"]


def test_persistence_after_restart(qtbot, tmp_path, quiet):
    reg = _registry(tmp_path)
    win = _window(qtbot, reg)
    win.ctl.new_project("Persist Me")
    win.dashboard.panels["codex"].count.setValue(4)
    win.dashboard.objective.setPlainText("Persisted objective")
    win.dashboard.save_objective()
    pid = win.ctl.project["id"]
    win.ctl.shutdown()
    win2 = _window(qtbot, reg)
    assert win2.ctl.project["id"] == pid
    assert win2.ctl.settings["providers"]["codex"]["count"] == 4
    assert win2.dashboard.objective.toPlainText() == "Persisted objective"


# ------------------------------------------------------------------ folder buttons
def test_folder_buttons_open_correct_locations(qtbot, tmp_path, quiet, opened):
    reg = _registry(tmp_path)
    win = _window(qtbot, reg)
    win.ctl.new_project("Calculator App")
    win.dashboard.refresh_project_card()
    ws = Path(win.ctl.project["workspace"])
    fb = win.dashboard.folder_buttons
    fb["Open Project Folder"].click()
    fb["Open Output Folder"].click()
    fb["Open Application Folder"].click()
    assert [Path(u) for u in opened] == [ws.resolve(), (ws / "output").resolve(),
                                         paths.app_install_dir()]
    assert (ws / "output").is_dir()                                 # created on demand
    assert all(Path(u).is_absolute() for u in opened)


def test_open_path_uses_native_file_url(opened, tmp_path):
    assert desktop.open_path(tmp_path)
    assert Path(opened[-1]) == tmp_path.resolve()
    assert not desktop.open_path("relative/folder")                 # never relative to cwd
    assert not desktop.open_path(tmp_path / "missing")


def test_settings_about_opens_application_folder(qtbot, tmp_path, quiet, opened):
    from triagent.ui.settings_dialog import SettingsDialog
    win = _window(qtbot, _registry(tmp_path))
    win.ctl.new_project("X")
    dlg = SettingsDialog(win.ctl.settings, win, win.ctl.project["workspace"],
                         str(win.ctl.projects_root))
    qtbot.addWidget(dlg)
    assert dlg.app_folder_lbl.text() == str(paths.app_install_dir())
    dlg.app_folder_btn.click()
    assert Path(opened[-1]) == paths.app_install_dir()


# ------------------------------------------------------------------ invalid workspace UX
def test_invalid_workspace_recovery_use_default(qtbot, tmp_path, quiet, opened):
    reg = _registry(tmp_path)
    bad = paths.app_install_dir() / "dist" / "TriAgentStudio"
    proj = reg.create("calculator", str(bad))             # like the user's existing project
    repo = reg.open_repo(proj["id"])
    repo.save_prompt("shared", "KEEP ME", 0)
    repo.close()
    reg.touch(proj["id"])
    asked = []
    win = MainWindow(AppController(reg))
    qtbot.addWidget(win)
    win._choose = lambda title, text, opts: asked.append([k for k, _ in opts]) or "default"
    win.show()
    win.bootstrap()
    assert win.ctl.workspace_status()[0] == "app_folder"
    qtbot.waitUntil(lambda: bool(asked), timeout=5000)             # recovery offered at start
    assert asked[0] == ["default", "choose", "appfolder"]
    new_ws = Path(win.ctl.project["workspace"])
    assert new_ws.parent == win.ctl.projects_root and new_ws.name == "calculator"
    assert new_ws.is_dir() and win.ctl.workspace_status() is None
    assert win.ctl.prompts()["shared"] == "KEEP ME"                # project data preserved
    assert win.ctl.effective_permission() == (None, True)          # new folder: trust again
    win.dashboard.refresh_project_card()
    assert win.dashboard.banner.isHidden()


def test_invalid_workspace_banner_and_app_folder_still_open(qtbot, tmp_path, quiet, opened):
    reg = _registry(tmp_path)
    proj = reg.create("calc", str(paths.app_install_dir() / "dist" / "TriAgentStudio"))
    reg.touch(proj["id"])
    win = MainWindow(AppController(reg))
    qtbot.addWidget(win)
    win._choose = lambda *a: None                                  # user postpones
    win.show()
    win.bootstrap()
    win.dashboard.refresh_project_card()
    assert not win.dashboard.banner.isHidden()
    assert "application folder" in win.dashboard.banner_text.text()
    assert not win.dashboard.folder_buttons["Open Project Folder"].isEnabled()
    assert win.open_app_folder()                                   # access is never denied
    assert Path(opened[-1]) == paths.app_install_dir()


def test_missing_workspace_recreate(qtbot, tmp_path, quiet):
    reg = _registry(tmp_path)
    ctl = AppController(reg)
    p = ctl.new_project("Gone")
    ws = Path(p["workspace"])
    ws.rmdir()
    ctl.shutdown()
    win = MainWindow(AppController(reg))
    qtbot.addWidget(win)
    win._choose = lambda title, text, opts: "recreate" if "recreate" in dict(opts) else None
    win.show()
    win.bootstrap()
    qtbot.waitUntil(lambda: ws.is_dir(), timeout=5000)
    assert win.ctl.workspace_status() is None


def test_start_with_invalid_workspace_offers_recovery(qtbot, tmp_path, quiet):
    reg = _registry(tmp_path)
    proj = reg.create("calc", str(paths.app_install_dir() / "dist" / "TriAgentStudio"))
    reg.touch(proj["id"])
    win = MainWindow(AppController(reg))
    qtbot.addWidget(win)
    calls = []
    win._choose = lambda *a: calls.append(1) and None
    win.show()
    win.bootstrap()
    n = len(calls)
    win.start()
    assert len(calls) == n + 1 and not win.ctl.engines                # nothing started


# ------------------------------------------------------------------ migration
def test_existing_project_migration_keeps_everything(tmp_path):
    home = tmp_path / "home"
    reg = ProjectRegistry(home)
    elsewhere = tmp_path / "old_place"
    elsewhere.mkdir()
    (elsewhere / "work.py").write_text("print(1)")
    proj = reg.create("Old", str(elsewhere))
    dbp = reg.project_dir(proj["id"]) / "project.db"
    # simulate an old database: tasks table without the newer approval_grant column
    con = sqlite3.connect(dbp)
    con.execute("ALTER TABLE tasks DROP COLUMN approval_grant")
    con.execute("INSERT INTO tasks(id, run_id, title, kind, status) VALUES('task-old', 'run-x', "
                "'legacy', 'implement', 'COMPLETED')")
    con.commit()
    con.close()
    tr = reg.project_dir(proj["id"]) / "runs" / "run-00000001" / "transcripts"
    tr.mkdir(parents=True)
    (tr / "t.jsonl").write_text("{}")
    ctl = AppController(reg)
    ctl.open_project(proj["id"])
    assert Path(ctl.project["workspace"]) == elsewhere.resolve()      # never moved
    assert (elsewhere / "work.py").read_text() == "print(1)"
    assert ctl.repo.get_task("task-old")["title"] == "legacy"
    cols = {r[1] for r in ctl.repo.store.q("PRAGMA table_info(tasks)")}
    assert "approval_grant" in cols
    assert (tr / "t.jsonl").exists()


def test_projects_survive_rebuild_location(tmp_path):
    """Project registry lives in the app data folder, not next to the executable."""
    reg = ProjectRegistry()
    assert not str(paths.app_data_dir()).startswith(str(paths.app_install_dir() / "dist"))
    assert Path(reg.root) == paths.app_data_dir()


# ------------------------------------------------------------------ packaged exe paths
def test_packaged_executable_path_resolution(tmp_path, monkeypatch):
    app = tmp_path / "TriAgentStudio"
    (app / "_internal" / "assets").mkdir(parents=True)
    exe = app / "TriAgentStudio.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(app / "_internal"), raising=False)
    assert paths.is_frozen()
    assert paths.app_install_dir() == app.resolve()
    assert paths.app_executable() == exe.resolve()
    assert paths.resource_path("assets", "x.ico") == app / "_internal" / "assets" / "x.ico"
    assert workspace_issue(app)[0] == "app_folder"
    assert workspace_issue(app / "_internal")[0] == "app_folder"
    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)                                          # cwd must not matter
        assert paths.app_install_dir() == app.resolve()
    finally:
        os.chdir(cwd)


def test_task_artifact_links(qtbot, tmp_path, quiet, opened, harness):
    h = harness()
    run_id = h.start()
    assert wait_for(lambda: h.run_status(run_id) == "completed", 90)
    ctl = AppController(h.registry)
    ctl.open_project(h.pid)
    from triagent.ui.tasks_view import TasksView
    v = TasksView(ctl)
    qtbot.addWidget(v)
    design = next(t for t in ctl.repo.tasks(run_id) if t["kind"] == "design")
    html = v._files_html(design)
    assert "DESIGN.md" in html and "file:///" in html
    v._open_link(QUrl.fromLocalFile(str(Path(ctl.project["workspace"]) / "DESIGN.md")))
    assert opened and Path(opened[-1]).name == "DESIGN.md"
    v._open_link(QUrl.fromLocalFile("C:/Windows/notepad.exe"))       # outside: ignored
    assert Path(opened[-1]).name == "DESIGN.md"
    ctl.shutdown()

"""GUI tests (offscreen Qt) covering the acceptance workflow with mock providers."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QMainWindow, QMessageBox

from triagent.core.projects import ProjectRegistry
from triagent.ui.controller import AppController
from triagent.ui.main_window import MainWindow
from triagent.ui.prompt_editor import PromptEditorWindow
from triagent.ui.theme import STYLESHEET


@pytest.fixture
def app_window(qtbot, tmp_path, workspace, monkeypatch):
    # accept every confirmation: return the dialog's default button when given, else Yes
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: a[4] if len(a) > 4 else QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)
    reg = ProjectRegistry(tmp_path / "home")
    reg.set("setup_done", "1")
    ctl = AppController(reg)
    proj = reg.create("UI project", str(workspace))
    repo = reg.open_repo(proj["id"])
    repo.set_setting("mock_mode", True)
    repo.set_setting("permission_mode", "full")                 # trusted workspace
    repo.set_setting("trusted_workspace", str(workspace.resolve()))
    repo.close()
    win = MainWindow(ctl)
    win.setStyleSheet(STYLESHEET)
    qtbot.addWidget(win)
    win.show()
    win.bootstrap()
    qtbot.waitUntil(lambda: all(i is not None for i in ctl.provider_info.values()), timeout=20000)
    yield win
    for e in ctl.engines.values():
        e.shutdown(5)


def test_main_window_and_agent_counts_persist(app_window, qtbot):
    win = app_window
    d = win.dashboard
    assert d.findChild(type(d.conc)) is not None
    for p, n in (("claude", 25), ("codex", 25)):
        d.panels[p].count.setValue(n)
    assert d.total.value.text() == "50"
    ag = d.panels["antigravity"]
    assert ag.isVisible() and ag.badge.text() == "UNSUPPORTED"
    assert not ag.enabled.isEnabled() and not ag.count.isEnabled() and not ag.test_btn.isEnabled()
    assert ag.config() == {"enabled": False, "count": 0, "model": ""}
    d.conc.setValue(5)
    pid = win.ctl.project["id"]
    # reopen the project from a fresh controller: settings restored
    ctl2 = AppController(win.ctl.registry)
    ctl2.open_project(pid)
    assert [ctl2.settings["providers"][p]["count"] for p in ("claude", "codex", "antigravity")] \
        == [25, 25, 0]
    assert ctl2.settings["global_concurrency"] == 5
    for p in ("claude", "codex"):
        assert d.panels[p].badge.text().startswith("READY")


def test_prompt_editor_is_separate_window_and_persists(app_window, qtbot):
    win = app_window
    win.open_prompt_editor()
    pe = win.prompt_window
    assert isinstance(pe, PromptEditorWindow) and isinstance(pe, QMainWindow)
    assert pe.isWindow() and pe.parent() is None and pe.isVisible()
    big = "line\n" * 50000
    pe.panes["shared"].edit.setPlainText("SHARED " + big)
    pe.panes["claude"].edit.setPlainText("CLAUDE ROLE")
    pe.panes["codex"].edit.setPlainText("CODEX ROLE")
    pe.panes["antigravity"].edit.setPlainText("AGY ROLE")
    assert pe.dirty()
    assert pe.save()
    assert not pe.dirty()
    pe.close()
    pe2 = PromptEditorWindow(win.ctl)
    qtbot.addWidget(pe2)
    assert pe2.panes["shared"].text() == "SHARED " + big          # no truncation
    assert pe2.panes["codex"].text() == "CODEX ROLE"


def test_prompt_editor_never_silently_discards(app_window, qtbot, monkeypatch):
    win = app_window
    win.open_prompt_editor()
    pe = win.prompt_window
    pe.panes["codex"].edit.setPlainText("unsaved work")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Cancel)
    pe.close()
    assert pe.isVisible() and pe.panes["codex"].text() == "unsaved work"
    # applying a template over existing text asks first; "No" keeps the text
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    pe.apply_template(next(iter(__import__("triagent.ui.templates", fromlist=["T"]).TEMPLATES)))
    assert pe.panes["codex"].text() == "unsaved work"


def test_acceptance_flow_with_mock_providers(app_window, qtbot):
    """Steps 1-11 of the acceptance scenario, using mock CLI processes."""
    win = app_window
    ctl = win.ctl
    d = win.dashboard
    for p in ("claude", "codex", "antigravity"):
        d.panels[p].count.setValue(2)
    d.objective.setPlainText("Build a tiny calculator module with tests.")
    win.open_prompt_editor()
    pe = win.prompt_window
    pe.panes["shared"].edit.setPlainText("Shared rules for everyone.")
    pe.panes["claude"].edit.setPlainText("Claude: architect.")
    pe.panes["codex"].edit.setPlainText("Codex: implementer.")
    pe.panes["antigravity"].edit.setPlainText("Antigravity: reviewer.")
    pe._start()                       # saves prompts and emits start_requested -> win.start
    qtbot.waitUntil(lambda: ctl.engine is not None and ctl.engine.run_id is not None, timeout=10000)
    run_id = ctl.engine.run_id
    assert win.stack.currentWidget() is win.collab
    qtbot.waitUntil(lambda: ctl.repo.get_run(run_id)["status"] == "completed", timeout=120000)
    win._refresh_now()
    win.collab.refresh()
    # live view shows real messages between providers
    assert win.collab.tree.topLevelItemCount() > 10
    msgs = ctl.repo.messages(run_id)
    assert {m["sender_provider"] for m in msgs} == {"claude", "codex"}
    texts = [win.collab.tree.topLevelItem(i).text(5) for i in
             range(win.collab.tree.topLevelItemCount())]
    assert "Message" in texts and "Agent started" in texts and "Collaboration completed" in texts
    # monitor
    win.nav.setCurrentRow(2)
    win._refresh_now()
    assert len(win.monitor.cards) == 4          # Antigravity unsupported: not registered
    assert int(win.monitor.t["completed"].value.text()) >= 10
    # tasks view
    win.nav.setCurrentRow(3)
    win._refresh_now()
    assert win.tasks.tree.topLevelItemCount() == len(ctl.repo.tasks(run_id))
    # the prompts used by the run were persisted with the run
    cfg = ctl.repo.get_run(run_id)["config"]
    assert cfg["prompts"]["codex"] == "Codex: implementer."
    # filters
    win.collab.f_type.setCurrentIndex(1)          # messages only
    assert all(win.collab.tree.topLevelItem(i).text(5) in ("Message", "Delivered")
               for i in range(win.collab.tree.topLevelItemCount()))
    # history shows the run; results export works
    win.nav.setCurrentRow(5)
    win._refresh_now()
    assert win.history.runs.rowCount() == 1


def test_stop_all_and_resume_from_gui(app_window, qtbot, monkeypatch):
    monkeypatch.setenv("MOCK_DELAY", "0.4")
    win = app_window
    ctl = win.ctl
    win.dashboard.objective.setPlainText("Something long running")
    win.start()
    qtbot.waitUntil(lambda: ctl.engine and ctl.engine.sup.active_count() > 0, timeout=20000)
    run_id = ctl.engine.run_id
    win.emergency_stop()
    qtbot.waitUntil(lambda: ctl.repo.get_run(run_id)["status"] == "stopped", timeout=20000)
    assert ctl.engine.sup.active_count() == 0
    monkeypatch.setenv("MOCK_DELAY", "0.01")
    ctl.view_run_id = run_id
    warnings_seen = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *a, **k: warnings_seen.append(a[1:3]) or QMessageBox.Ok)
    win.resume()
    # regression: resuming immediately after "stopped" must not race the engine thread
    assert warnings_seen == []
    qtbot.waitUntil(lambda: ctl.repo.get_run(run_id)["status"] == "completed", timeout=120000)


def test_settings_dialog_roundtrip(app_window, qtbot):
    from triagent.ui.settings_dialog import SettingsDialog
    dlg = SettingsDialog(app_window.ctl.settings)
    qtbot.addWidget(dlg)
    dlg.global_conc.setValue(9)
    dlg.shell.setText("Bash(pytest:*)")
    dlg._save()
    v = dlg.value()
    assert v["global_concurrency"] == 9 and v["claude_shell_patterns"] == ["Bash(pytest:*)"]


def test_all_pages_render(app_window, qtbot):
    win = app_window
    for i in range(win.nav.count()):
        win.nav.setCurrentRow(i)
        win._refresh_now()
    win.diag.refresh()
    assert win.diag.table.rowCount() > 5



def test_permission_mode_persists_and_is_required(app_window, qtbot, tmp_path):
    win = app_window
    ctl = win.ctl
    assert ctl.effective_permission() == ("full", False)
    assert win.dashboard.perm.currentData() == "full"
    ctl.set_permission_mode("manual")
    ctl2 = AppController(ctl.registry)                       # "restart"
    ctl2.open_project(ctl.project["id"])
    assert ctl2.effective_permission() == ("manual", False)
    # a new, never-trusted workspace must be decided explicitly before agents start
    ws2 = tmp_path / "ws_new"
    ws2.mkdir()
    ctl2.create_project("new", str(ws2))
    assert ctl2.effective_permission() == (None, True)
    with pytest.raises(RuntimeError):
        ctl2.start_collaboration()


def test_approvals_tab_live_approve_once(app_window, qtbot, monkeypatch):
    """A real control request from the (mock) Claude CLI appears in the Approvals tab with a
    counter and a desktop notification; Approve Once is delivered to the waiting process."""
    monkeypatch.setenv("MOCK_ASK", "Bash:git push origin main")
    monkeypatch.setenv("MOCK_ASK_KINDS", "design")
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Yes)
    win = app_window
    ctl = win.ctl
    win.dashboard.objective.setPlainText("Calculator")
    win.start()
    qtbot.waitUntil(lambda: bool(ctl.engine and ctl.engine.run_id), timeout=10000)
    run_id = ctl.engine.run_id
    qtbot.waitUntil(lambda: win.nav.item(4).text() == "Approvals (1)", timeout=30000)
    assert getattr(win, "last_notification", ("", ""))[0] == "Approval needed"
    win.nav.setCurrentRow(4)
    win._refresh_now()
    view = win.approvals
    assert view.table.rowCount() == 1
    assert "git push" in view.table.item(0, 4).text() and view.table.item(0, 6).text() == "HIGH"
    view.table.selectRow(0)
    assert view.b_approve.isEnabled() and view.b_reject.isEnabled()
    view.b_approve.click()
    qtbot.waitUntil(lambda: ctl.repo.get_run(run_id)["status"] == "completed", timeout=60000)
    a = ctl.repo.approvals(run_id)[0]
    assert a["status"] == "approved" and a["decided_by"] == "user"
    task = ctl.repo.get_task(a["task_id"])
    assert "PERMISSION_DECISION=allow" in task["result_summary"]
    win._check_approvals()
    assert win.nav.item(4).text() == "Approvals"
    view.filter.setCurrentIndex(1)                                    # persisted history
    assert view.table.rowCount() == 1 and view.table.item(0, 8).text() == "approved"

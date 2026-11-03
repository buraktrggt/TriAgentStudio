"""Application entry point."""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    # The packaged executable doubles as the mock agent process used for quota-free testing.
    if "--mock-agent" in argv:
        from .providers.mock_agent import main as mock_main
        try:
            return mock_main([a for a in argv[1:] if a != "--mock-agent"])
        except BaseException as e:  # never let the windowed bootloader show a modal dialog
            try:
                sys.stderr.write(f"mock agent error: {e}\n")
            except Exception:
                pass
            return 1

    if "--selftest-folders" in argv:
        only = argv[argv.index("--only") + 1] if "--only" in argv else None
        return selftest_folders(argv[argv.index("--selftest-folders") + 1], only)

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from . import APP_NAME, paths
    from .ui.main_window import MainWindow
    from .ui.theme import STYLESHEET

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TriAgentStudio.App")
        except Exception:
            pass
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("TriAgentStudio")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    icon = paths.resource_path("assets", "triagent.ico")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    win = MainWindow()
    win.show()
    win.bootstrap()
    return app.exec()


def selftest_folders(out_file: str, only: str | None = None) -> int:
    """Packaged-app smoke test: start the real window, restore/create a project and click the
    real folder buttons. Writes what was opened (and what was expected) to `out_file`.
    Use a separate TRIAGENT_HOME / TRIAGENT_PROJECTS_DIR to keep user data untouched."""
    import json
    import time
    from pathlib import Path

    from PySide6.QtWidgets import QApplication, QMessageBox

    from . import paths
    from .ui import desktop
    from .ui.main_window import MainWindow
    from .ui.theme import STYLESHEET

    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)
    app = QApplication([sys.argv[0]])
    app.setStyleSheet(STYLESHEET)
    win = MainWindow()
    win._choose = lambda *a: None          # never block on a modal recovery prompt
    win.ctl.registry.set("setup_done", "1")
    win.show()
    win.bootstrap()
    created = False
    if not win.ctl.project:
        win.ctl.new_project("Selftest Project")
        created = True
    t0 = time.time()
    while time.time() - t0 < 1.0:
        app.processEvents()
    dash = win.dashboard
    dash.refresh_project_card()
    results = {}
    for name in ("Open Project Folder", "Open Output Folder", "Open Application Folder"):
        if only and name != only:
            continue
        before = len(desktop.OPEN_LOG)
        dash.folder_buttons[name].click()
        app.processEvents()
        results[name] = desktop.OPEN_LOG[before:]
    report = {
        "frozen": paths.is_frozen(), "executable": sys.executable,
        "project_created": created, "project": win.ctl.project["name"],
        "expected": {"Open Project Folder": str(Path(win.ctl.project["workspace"]).resolve()),
                     "Open Output Folder": str(paths.output_dir(win.ctl.project["workspace"])
                                               .resolve()),
                     "Open Application Folder": str(paths.app_install_dir())},
        "opened": results, "projects_root": str(win.ctl.projects_root),
        "data_dir": str(paths.app_data_dir()),
    }
    Path(out_file).write_text(json.dumps(report, indent=2), encoding="utf-8")
    win.ctl.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())

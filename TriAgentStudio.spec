# PyInstaller spec for TriAgent Studio (one-folder build: fast start, no temp extraction).
# Build with:  .venv\Scripts\pyinstaller TriAgentStudio.spec --noconfirm
block_cipher = None

a = Analysis(
    ["launch_triagent.py"],
    pathex=["."],
    binaries=[],
    datas=[("assets/triagent.ico", "assets"), ("assets/triagent.png", "assets")],
    hiddenimports=["triagent.providers.mock_agent"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
              "PySide6.Qt3DCore", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia",
              "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="TriAgentStudio",
    icon="assets/triagent.ico",
    console=False,
    debug=False,
    strip=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="TriAgentStudio")

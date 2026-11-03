"""Generate assets/triagent.ico (three interlocking provider-coloured nodes)."""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen

ROOT = Path(__file__).resolve().parent.parent


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#151826"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(0, 0, size, size, size * 0.22, size * 0.22)
    c = size / 2
    r = size * 0.27
    pts = [QPointF(c, c - r), QPointF(c - r * 0.87, c + r * 0.5), QPointF(c + r * 0.87, c + r * 0.5)]
    p.setPen(QPen(QColor("#7C8CFF"), max(1.0, size * 0.045)))
    for i in range(3):
        p.drawLine(pts[i], pts[(i + 1) % 3])
    p.setPen(Qt.NoPen)
    for pt, col in zip(pts, ("#E0855F", "#19B98A", "#5B9BFF")):
        p.setBrush(QColor(col))
        p.drawEllipse(pt, size * 0.13, size * 0.13)
    p.end()
    return img


if __name__ == "__main__":
    app = QGuiApplication(sys.argv)
    out = ROOT / "assets"
    out.mkdir(exist_ok=True)
    render(256).save(str(out / "triagent.ico"))
    render(256).save(str(out / "triagent.png"))
    print("written", out / "triagent.ico")

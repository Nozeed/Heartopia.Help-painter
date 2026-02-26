import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


try:
    from heartopia_painter.app import run
except ModuleNotFoundError as e:
    missing = getattr(e, "name", None)
    if missing in {"PySide6", "pillow", "PIL", "mss", "pynput", "pyautogui"} or (
        isinstance(missing, str) and missing.startswith("PySide6")
    ):
        sys.stderr.write(
            "ขาด Python dependencies\n\n"
            "โดยทั่วไปหมายถึงคุณกำลังรันด้วย Python interpreter ที่ผิด (ไม่ใช่ venv ของโปรเจค)\n\n"
            "แก้ไข:\n"
            "  1) เปิดใช้งาน venv: .\\.venv\\Scripts\\Activate.ps1\n"
            "  2) ติดตั้ง deps:     python -m pip install -r requirements.txt\n"
            "  3) รัน:              python main.py\n\n"
            "หรือรันโดยตรงด้วย venv python:\n"
            "  .\\.venv\\Scripts\\python.exe main.py\n\n"
            "https://beer-studio.com"
        )
        raise SystemExit(1)
    raise


if __name__ == "__main__":
    run()

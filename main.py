"""SimpleLocalChatbot エントリポイント。

実行: python main.py
"""
import logging
import sys

from PySide6.QtWidgets import QApplication

from core.app_diagnostics import enable_diagnostics
from ui.main_window import MainWindow, default_chat_path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def main() -> int:
    app = QApplication(sys.argv)
    chat_path = default_chat_path()
    try:
        enable_diagnostics(chat_path.parent / "logs")
    except OSError:
        logging.exception("診断ログを作成できません。コンソールへの記録を続けます。")
    window = MainWindow(chat_path=chat_path)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())


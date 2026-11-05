"""SimpleLocalChatbot メインウィンドウ。"""
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMainWindow, QMessageBox, QVBoxLayout, QWidget

from ui.application_style import apply_application_style
from ui.chat_panel import ChatPanel
from ui.value_wheel_guard import install_value_wheel_guard

APP_NAME = "SimpleLocalChatbot"
APP_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = APP_ROOT / "data"
CHAT_FILE_NAME = "chat.json"


def default_chat_path() -> Path:
    """アプリフォルダ内（data/chat.json）の既定のチャット保存ファイルパスを返す。"""
    return DATA_DIR / CHAT_FILE_NAME


class MainWindow(QMainWindow):
    """ローカルLLMチャット単体アプリのメインウィンドウ。"""

    def __init__(self, chat_path: Path | str | None = None, *, auto_start: bool = True) -> None:
        super().__init__()
        apply_application_style()
        install_value_wheel_guard()
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 720)
        self._close_when_finished = False
        self._chat_path = Path(chat_path) if chat_path else default_chat_path()

        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        self._chat_panel = ChatPanel(
            self._chat_path,
            prepare=self._prepare_chat,
            parent=central,
            auto_start=auto_start,
        )
        root.addWidget(self._chat_panel, 1)
        self._chat_panel.service.busyChanged.connect(self._chat_busy_changed)

    @property
    def chat_panel(self) -> ChatPanel:
        return self._chat_panel

    def _chat_busy(self) -> bool:
        return self._chat_panel.service.is_running

    def _prepare_chat(self) -> None:
        if self._close_when_finished or self._chat_busy():
            raise RuntimeError("前のチャット処理またはモデル解放が終了してから送信してください。")

    def _chat_busy_changed(self, _busy: bool) -> None:
        if self._close_when_finished and not self._chat_panel.service.is_active:
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._chat_panel.service.is_active:
            self._close_when_finished = True
            self._chat_panel.service.cancel()
            event.ignore()
            return
        if not self._chat_panel.flush():
            QMessageBox.warning(
                self,
                "チャットの保存エラー",
                "会話を保存できません。チャット画面のエラーを確認し、内容をコピーして保管してください。",
            )
            event.ignore()
            return
        self._chat_panel.shutdown()
        super().closeEvent(event)


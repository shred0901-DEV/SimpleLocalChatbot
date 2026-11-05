"""Japanese chat workspace with optional, externally installed Ollama."""
from html import escape
import time

from PySide6.QtCore import QEvent, QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QTextDocument
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QScrollArea, QSizePolicy, QSpinBox, QTextBrowser, QVBoxLayout, QWidget,
)

from config.chat_store import ChatStore
from core.chat import ChatConversation, ChatSettings, ChatTurn, MAX_TURNS, conversation, server_url
from core.chat_search import valid_source_url
from core.chat_images import IMAGE_ONLY_PROMPT, MAX_IMAGES
from services.chat_service import ChatService
from services.ollama_account_service import OllamaAccountService
from ui.chat_attachments import ChatAttachments, ChatMessageInput, attachment_mime, clipboard_bytes, thumbnail


class ChatPanel(QWidget):
    def __init__(self, path, prepare=None, parent=None, service=None, *, account_service=None, auto_start=True):
        super().__init__(parent)
        self.store = ChatStore(path)
        self.prepare = prepare or (lambda: None)
        self.service = service or ChatService(self)
        self.account_service = account_service or OllamaAccountService(self)
        self._closed = False
        self._used_search = False
        self._ime_composing = False
        self._thumbnail_cache = {}
        self.setAcceptDrops(True)
        self._current = None
        self._dirty = False
        self._connecting = False
        self._connected_endpoint = ''
        self._models = []
        self._viewport = None
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self.flush)
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(40)
        self._render_timer.timeout.connect(self._render)

        layout = QHBoxLayout(self)
        self.control_panel = QScrollArea()
        self.control_panel.setWidgetResizable(True)
        self.control_panel.setFrameShape(QScrollArea.Shape.NoFrame)
        control_content = QWidget()
        self.control_panel.setWidget(control_content)
        self.chat_area = QWidget()
        self.chat_area.setMinimumWidth(0)
        self.chat_area.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.control_panel)
        layout.addWidget(self.chat_area, 1)
        controls = QVBoxLayout(control_content)
        chat = QVBoxLayout(self.chat_area)
        controls.setContentsMargins(0, 0, 0, 0)
        chat.setContentsMargins(0, 0, 0, 0)
        controls.addWidget(QLabel('コントロールパネル'))
        self.new = QPushButton('新しい会話')
        controls.addWidget(self.new)
        controls.addWidget(QLabel('会話の履歴（選択すると再開できます）'))
        self.history = QListWidget()
        self.history.setMinimumWidth(0)
        self.history.setMinimumHeight(100)
        self.history.setTextElideMode(Qt.TextElideMode.ElideRight)
        controls.addWidget(self.history, 1)
        search_note = QLabel('1送信につき検索1回・最大3件。利用条件はOllama側に従います。')
        search_note.setWordWrap(True)
        self.search_note = search_note
        self.control_panel.setFixedWidth(search_note.fontMetrics().horizontalAdvance(search_note.text()) + 4)
        controls.addWidget(search_note)

        credit_group = QGroupBox('Ollamaクレジット（USD）')
        credit_layout = QVBoxLayout(credit_group)
        self.balance = QLabel('残量: 未取得')
        self.usage = QLabel('消費量: 未取得')
        self.credit_status = QLabel('')
        for label in (self.balance, self.usage, self.credit_status):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            credit_layout.addWidget(label)
        credit_note = QLabel('消費量は本人の全API利用額です。\n反映に時間がかかる場合があります。')
        credit_note.setToolTip('他のアプリからの利用も含みます。残量はチーム等の共有枠を含む場合があります。')
        credit_note.setWordWrap(True)
        self.credit_note = credit_note
        credit_layout.addWidget(credit_note)
        self.credit_refresh = QPushButton('クレジット情報を更新')
        credit_layout.addWidget(self.credit_refresh)
        controls.addWidget(credit_group)

        guide = QLabel(
            '初回は <a href="https://ollama.com/download/windows">Ollama公式サイト</a> からインストールし、'
            'PowerShellで <b>ollama pull gemma4:e4b</b> を実行してください。'
            'Ollamaを起動後、「接続確認・モデル再読込」でモデルを選びます。'
            '<br>会話履歴と入力途中の文章はこのアプリ内に保存します。長い会話では古いやり取りを送信対象から外します。'
            '応答後と中止・失敗時にはモデルのメモリ解放をOllamaへ依頼します。')
        guide.setWordWrap(True)
        guide.setOpenExternalLinks(True)
        guide.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self._guide = guide
        controls.addWidget(guide)

        chat.addWidget(QLabel('チャット'))
        self.transcript = QTextBrowser()
        self.transcript.setAcceptDrops(False)
        self.transcript.viewport().setAcceptDrops(False)
        self.transcript.setOpenLinks(False)
        self.transcript.anchorClicked.connect(self._open_source)
        self.transcript.setPlaceholderText('日本語でメッセージを入力してください。')
        chat.addWidget(self.transcript, 1)
        self.attachments = ChatAttachments(self.store.images)
        self.attachments.set_images(self.store.active.draft_images)
        chat.addWidget(self.attachments)
        self.input = ChatMessageInput()
        self.input.setPlaceholderText('メッセージを入力（Enterで送信、Shift＋Enterで改行）')
        self.input.setMaximumHeight(120)
        self.input.setMinimumHeight(70)
        self.input.setPlainText(self.store.draft)
        self.input.installEventFilter(self)
        chat.addWidget(self.input)
        self.connection_scroll = QScrollArea()
        self.connection_scroll.setWidgetResizable(True)
        self.connection_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.connection_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.connection_row = QWidget()
        self.connection_scroll.setWidget(self.connection_row)
        self.connection_scroll.installEventFilter(self)
        self.connection_row.installEventFilter(self)
        connection = QHBoxLayout(self.connection_row)
        connection.setContentsMargins(0, 0, 0, 0)
        connection.setSpacing(4)
        connection.addWidget(QLabel('Ollama接続先'))
        self.endpoint = QLineEdit(self.store.settings.endpoint)
        self.endpoint.setFixedWidth(self.endpoint.fontMetrics().horizontalAdvance('http://127.0.0.1:11434') + 10)
        self.endpoint.setToolTip('Ollama接続先。変更後は右の「接続確認・モデル再読込」を押してください。')
        connection.addWidget(self.endpoint)
        connection.addWidget(QLabel('モデル'))
        self.model = QComboBox()
        self.model.setMinimumContentsLength(6)
        self.model.setMinimumWidth(160)
        self.model.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.model.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        if self.store.settings.model:
            self.model.addItem(self.store.settings.model)
        connection.addWidget(self.model, 1)
        self.refresh = QPushButton('接続確認・モデル再読込')
        connection.addWidget(self.refresh)
        self.web_search = QCheckBox('Web検索を使う')
        self.web_search.setChecked(self.store.settings.web_search)
        self.web_search.setToolTip('Ollama Web Search。環境変数 OLLAMA_API_KEY が必要です。入力文を検索語として外部へ送信します。')
        connection.addWidget(self.web_search)
        connection.addWidget(QLabel('会話の長さ'))
        self.context = QSpinBox()
        self.context.setRange(2048, 32768)
        self.context.setSingleStep(1024)
        self.context.setValue(self.store.settings.num_ctx)
        self.context.setSuffix(' tokens')
        self.context.setToolTip('増やすとメモリ使用量が増えます。長い会話は直近の完了したやり取りから送信します。')
        connection.addWidget(self.context)
        chat.addWidget(self.connection_scroll)
        self._fit_connection_row()
        buttons = QHBoxLayout()
        self.attach = QPushButton('画像を添付')
        self.attach.setToolTip('最大3枚。入力欄への画像の貼り付け・ドロップも可能です。送信画像は長辺1600px以下に縮小します。')
        buttons.addWidget(self.attach)
        self.send = QPushButton('送信')
        self.cancel = QPushButton('中止')
        self.retry = QPushButton('モデル解放を再試行')
        self.retry.hide()
        buttons.addStretch(1)
        buttons.addWidget(self.retry)
        buttons.addWidget(self.send)
        buttons.addWidget(self.cancel)
        chat.addLayout(buttons)
        self.status = QLabel('起動時にOllamaへ自動接続します。')
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        chat.addWidget(self.status)
        self.save_status = QLabel(self.store.error)
        self.save_status.setTextFormat(Qt.TextFormat.PlainText)
        self.save_status.setWordWrap(True)
        self.save_status.setToolTip(str(self.store.path))
        chat.addWidget(self.save_status)

        self.refresh.clicked.connect(self.refresh_models)
        self.attach.clicked.connect(self._choose_images)
        self.attachments.removeRequested.connect(self._remove_image)
        self.input.filesReceived.connect(self._add_image_files)
        self.input.imageReceived.connect(self._add_clipboard_image)
        self.input.attachmentFailed.connect(self.status.setText)
        self.send.clicked.connect(self.send_message)
        self.cancel.clicked.connect(self.service.cancel)
        self.retry.clicked.connect(self.service.retry_release)
        self.new.clicked.connect(self.new_conversation)
        self.history.currentItemChanged.connect(self._select_conversation)
        self.credit_refresh.clicked.connect(self.refresh_credits)
        self.endpoint.textEdited.connect(self._endpoint_changed)
        self.model.currentTextChanged.connect(self._changed)
        self.context.valueChanged.connect(self._changed)
        self.web_search.toggled.connect(self._changed)
        self.input.textChanged.connect(self._changed)
        self.service.modelsReady.connect(self._models_ready)
        self.service.connectionFailed.connect(self._connection_failed)
        self.service.connectingChanged.connect(self._connecting_changed)
        self.service.textReceived.connect(self._text_received)
        self.service.sourcesReady.connect(self._sources_received)
        self.service.result.connect(self._result)
        self.service.status.connect(self.status.setText)
        self.service.busyChanged.connect(self._update_state)
        self.account_service.updated.connect(self._credit_updated)
        self.account_service.failed.connect(self._credit_failed)
        self.account_service.busyChanged.connect(self._credit_busy_changed)
        self._credit_timer = QTimer(self)
        self._credit_timer.setSingleShot(True)
        self._credit_timer.timeout.connect(self._credit_cooldown_finished)
        self._credit_pending = False
        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.timeout.connect(self._startup)
        if auto_start:
            self._startup_timer.start(0)
        self._render_history()
        self._render()
        self._update_state()

    def fit_viewport(self, viewport, tab_bar):
        # Other workspaces have large minimum sizes. Keep the chat input/footer
        # inside the visible area even when the shared outer tabs must scroll.
        self._viewport, self._tab_bar = viewport, tab_bar
        viewport.installEventFilter(self)
        QTimer.singleShot(0, self._fit_viewport)

    def eventFilter(self, watched, event):
        if (watched in (getattr(self, 'connection_scroll', None), getattr(self, 'connection_row', None))
                and event.type() in (QEvent.Type.Resize, QEvent.Type.FontChange)):
            QTimer.singleShot(0, self._fit_connection_row)
        if watched is self.input:
            if event.type() == QEvent.Type.InputMethod:
                self._ime_composing = bool(event.preeditString())
            elif event.type() == QEvent.Type.FocusOut:
                self._ime_composing = False
            elif (event.type() == QEvent.Type.KeyPress
                  and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                  and not event.modifiers() & (Qt.KeyboardModifier.ShiftModifier
                                               | Qt.KeyboardModifier.ControlModifier
                                               | Qt.KeyboardModifier.AltModifier
                                               | Qt.KeyboardModifier.MetaModifier)):
                if self._ime_composing:
                    return True
                if not event.isAutoRepeat():
                    self.send_message()
                return True
        if watched is self._viewport and event.type() == QEvent.Type.Resize:
            self._fit_viewport()
        return super().eventFilter(watched, event)

    def _fit_connection_row(self):
        if self._closed:
            return
        # Keep the requested order on one line; narrow windows can scroll it.
        height = self.connection_row.minimumSizeHint().height()
        if self.connection_row.minimumSizeHint().width() > self.connection_scroll.viewport().width():
            height += self.connection_scroll.horizontalScrollBar().sizeHint().height()
        self.connection_scroll.setFixedHeight(height)

    def dragEnterEvent(self, event):
        if self._can_attach() and attachment_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if self._can_attach() and attachment_mime(event.mimeData()):
            self.input.insertFromMimeData(event.mimeData())
            event.acceptProposedAction()
        else:
            event.ignore()

    def _can_attach(self):
        return not self._closed and not self.store.read_failed and not self.service.is_running

    def _choose_images(self):
        if self._can_attach():
            files, _ = QFileDialog.getOpenFileNames(
                self, 'チャットに画像を添付', '', '画像 (*.png *.jpg *.jpeg *.webp *.bmp *.gif)')
            if files:
                self._add_image_files(files)

    def _append_images(self, images):
        existing = {image.id for image in self.store.active.draft_images}
        for image in images:
            if image.id not in existing:
                self.store.active.draft_images.append(image)
                existing.add(image.id)
        self.attachments.set_images(self.store.active.draft_images)
        self._changed()
        self.flush()

    def _add_image_files(self, files):
        if not self._can_attach():
            return
        try:
            if len(files) + len(self.store.active.draft_images) > MAX_IMAGES:
                raise ValueError('添付画像は1メッセージにつき3枚までです。')
            images = [self.store.images.add_file(filename) for filename in files]
            self._append_images(images)
        except ValueError as exc:
            self.status.setText(str(exc))

    def _add_clipboard_image(self, image):
        if not self._can_attach():
            return
        try:
            if len(self.store.active.draft_images) >= MAX_IMAGES:
                raise ValueError('添付画像は1メッセージにつき3枚までです。')
            attached = self.store.images.add_data(clipboard_bytes(image), '貼り付け画像')
            self._append_images([attached])
        except ValueError as exc:
            self.status.setText(str(exc))

    def _remove_image(self, index):
        if self._can_attach() and 0 <= index < len(self.store.active.draft_images):
            del self.store.active.draft_images[index]
            self.attachments.set_images(self.store.active.draft_images)
            self._changed()
            self.flush()

    def _startup(self):
        if self._closed:
            return
        if not self._connecting and not self._connected_endpoint:
            self.refresh_models()
        self.refresh_credits()

    def refresh_credits(self):
        if self._closed or self.account_service.is_running:
            return
        if time.monotonic() < self.account_service.next_refresh:
            return
        self._credit_pending = False
        self.credit_status.setText('取得しています…')
        self.account_service.refresh()

    def _credit_updated(self, kind, text):
        (self.balance if kind == 'balance' else self.usage).setText(text)

    def _credit_failed(self, kind, message):
        title = '残量' if kind == 'balance' else '消費量'
        (self.balance if kind == 'balance' else self.usage).setText(f'{title}: 取得できません。{message}')
        if not self.account_service.is_running:
            self.credit_status.setText('')

    def _credit_busy_changed(self, busy):
        self.credit_refresh.setEnabled(not busy)
        if not busy and not self._closed:
            from datetime import datetime
            self.credit_status.setText('最終確認: ' + datetime.now().strftime('%H:%M:%S')
                                       + '（次の更新まで約1分）')
            self.credit_refresh.setEnabled(False)
            delay = max(1, int((self.account_service.next_refresh - time.monotonic()) * 1000) + 1)
            self._credit_timer.start(delay)

    def _credit_cooldown_finished(self):
        if not self._closed:
            self.credit_refresh.setEnabled(True)
            if self._credit_pending:
                self.refresh_credits()

    def _render_history(self):
        self.history.blockSignals(True)
        entries = list(reversed(self.store.conversations))
        ids = [self.history.item(row).data(Qt.ItemDataRole.UserRole) for row in range(self.history.count())]
        if ids != [entry.id for entry in entries]:
            self.history.clear()
            for entry in entries:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, entry.id)
                self.history.addItem(item)
        for row, entry in enumerate(entries):
            item = self.history.item(row)
            item.setText(entry.title)
            item.setToolTip(f'{entry.title}\n{len(entry.turns)}件のやり取り')
            if entry.id == self.store.active_id:
                self.history.setCurrentItem(item)
        self.history.blockSignals(False)

    def _select_conversation(self, item, _previous=None):
        if item is None:
            return
        selected = item.data(Qt.ItemDataRole.UserRole)
        if selected == self.store.active_id:
            return
        if self.service.is_running or self._connecting or self.store.read_failed or not self.flush():
            self._render_history()
            return
        previous = self.store.active_id
        self.store.active_id = selected
        if not self.store.save():
            self.store.active_id = previous
            self.save_status.setText(self.store.error)
            self._render_history()
            return
        self.input.blockSignals(True)
        self.input.setPlainText(self.store.draft)
        self.input.blockSignals(False)
        self.attachments.set_images(self.store.active.draft_images)
        self.save_status.clear()
        self._render()
        self.transcript.verticalScrollBar().setValue(self.transcript.verticalScrollBar().maximum())
        self._update_state()

    def _fit_viewport(self):
        minimum = self.minimumSizeHint()
        self.setMaximumSize(max(minimum.width(), self._viewport.width() - 4),
                            max(minimum.height(), self._viewport.height() - self._tab_bar.height() - 4))

    def _settings(self):
        return ChatSettings(server_url(self.endpoint.text()), self.model.currentText(), self.context.value(),
                            web_search=self.web_search.isChecked())

    def _changed(self, *_args):
        self.store.draft = self.input.toPlainText()
        self._dirty = True
        self._save_timer.start()
        self._render_history()
        self._update_state()

    def _endpoint_changed(self):
        self._connected_endpoint = ''
        self.status.setText('接続先を変更しました。接続確認を行ってください。')
        self._changed()

    def _update_state(self, *_args):
        busy = self.service.is_running
        writable = not self.store.read_failed
        for control in (self.endpoint, self.model, self.context, self.web_search, self.new, self.history):
            control.setEnabled(writable and not busy and not self._connecting)
        self.refresh.setEnabled(writable and not busy and not self._connecting)
        self.send.setEnabled(writable and not busy and not self._connecting
                             and bool(self._connected_endpoint)
                             and bool(self.input.toPlainText().strip() or self.store.active.draft_images)
                             and self.model.currentText() in self._models)
        self.cancel.setEnabled(self.service.can_cancel)
        self.retry.setVisible(self.service.release_failed)
        self.retry.setEnabled(self.service.release_failed)
        self.input.setReadOnly(not writable)
        self.input.setMaximumHeight(80 if self.store.active.draft_images else 120)
        self.input.images_enabled = writable and not busy
        self.attach.setEnabled(writable and not busy)
        self.attachments.setEnabled(writable and not busy)

    def refresh_models(self):
        if self._closed or self.store.read_failed or self.service.is_running or self._connecting:
            return
        try:
            settings = self._settings()
            self._probe_endpoint = settings.endpoint
            self._connected_endpoint = ''
            if self.service.refresh_models(settings.endpoint):
                self.status.setText('Ollamaに接続しています…')
                self._changed()
        except ValueError as exc:
            self.status.setText(str(exc))

    def _connecting_changed(self, active):
        self._connecting = active
        self._update_state()

    def _models_ready(self, models):
        self._models = models
        self._guide.setVisible(not models)
        self._connected_endpoint = self._probe_endpoint
        selected = self.model.currentText() or self.store.settings.model
        self.model.blockSignals(True)
        self.model.clear()
        self.model.addItems(models)
        if selected in models:
            self.model.setCurrentText(selected)
        self.model.blockSignals(False)
        self.status.setText('接続しました。モデルを選んで会話を始めてください。' if models else
                            '接続しましたが、ローカルモデルがありません。上の導入手順でモデルを追加してください。')
        self._changed()

    def _connection_failed(self, message):
        self._connected_endpoint = ''
        self._guide.show()
        self.status.setText(message)
        self._update_state()

    def send_message(self):
        if self._closed or self.service.is_running or self._connecting or self.store.read_failed:
            return
        text = self.input.toPlainText().strip()
        images = list(self.store.active.draft_images)
        if not text and not images:
            return
        try:
            settings = self._settings()
            if settings.web_search and images and not text:
                raise ValueError('Web検索も使う場合は、検索したい内容を質問文として入力してください。')
            if settings.endpoint != self._connected_endpoint or settings.model not in self._models:
                raise ValueError('接続確認を行い、ダウンロード済みのモデルを選択してください。')
            if len(self.store.turns) >= MAX_TURNS:
                raise ValueError('会話履歴が100件になりました。「新しい会話」で会話を始め直してください。')
            messages, omitted = conversation(self.store.turns, text, settings.num_ctx, images)
            messages = self.store.images.prepare_messages(messages)
            self.prepare()
            self._current = ChatTurn(user=text or IMAGE_ONLY_PROMPT, model=settings.model, images=images)
            self.store.turns.append(self._current)
            self.store.active.draft_images = []
            self.attachments.set_images([])
            self.input.clear()
            self._dirty = True
            if not self.flush():
                self.store.turns.pop()
                self._current = None
                self.store.active.draft_images = images
                self.attachments.set_images(images)
                self.input.setPlainText(text)
                self._render()
                return
            self._render()
            self._render_history()
            self._used_search = settings.web_search
            self.service.start(settings, messages)
            if omitted and self.service.is_active and not settings.web_search:
                self.status.setText(f'応答を生成しています…（古い{omitted}件のやり取りは今回の送信対象から外しています）')
        except (ValueError, RuntimeError) as exc:
            self.status.setText(str(exc))

    def _text_received(self, text):
        if self._current is not None:
            self._current.assistant += text
            self._dirty = True
            if not self._render_timer.isActive():
                self._render_timer.start()
            if not self._save_timer.isActive():
                self._save_timer.start()

    def _sources_received(self, sources):
        if self._current is not None:
            self._current.sources = sources
            self._dirty = True
            self.flush()
            self._render()

    def _open_source(self, url):
        if valid_source_url(url.toString()):
            QDesktopServices.openUrl(url)

    def _result(self, state, text, error):
        if self._current is not None:
            self._current.assistant, self._current.state, self._current.error = text, state, error
            self._current = None
            self._dirty = True
            self.flush()
            self._render()
            self._render_history()
            if self._used_search:
                if time.monotonic() >= self.account_service.next_refresh:
                    self.refresh_credits()
                else:
                    self._credit_pending = True

    def _render(self):
        self._render_timer.stop()
        scroll = self.transcript.verticalScrollBar()
        position = scroll.value()
        at_bottom = position >= scroll.maximum() - 4
        parts = []
        for turn in self.store.turns:
            def plain(value):
                return escape(value).replace('\n', '<br>')
            parts.append(f'<p><b>あなた</b><br>{plain(turn.user)}</p>')
            for image in turn.images:
                try:
                    if image.id not in self._thumbnail_cache:
                        if len(self._thumbnail_cache) >= 32:
                            self._thumbnail_cache.pop(next(iter(self._thumbnail_cache)))
                        self._thumbnail_cache[image.id] = thumbnail(self.store.images, image)
                    preview = self._thumbnail_cache[image.id]
                    url = QUrl('chatimage:' + image.id)
                    self.transcript.document().addResource(QTextDocument.ResourceType.ImageResource, url, preview)
                    parts.append(f'<p><img src="{url.toString()}" width="{preview.width()}" '
                                 f'height="{preview.height()}"><br>{plain(image.name)}</p>')
                except ValueError:
                    parts.append(f'<p>添付画像を読み込めません: {plain(image.name)}</p>')
            suffix = ('（応答中）' if turn is self._current else '' if turn.state == 'complete' else
                      '（中断・未完了）' if turn.state == 'interrupted' else '（エラー）')
            parts.append(f'<p><b>アシスタント {plain(turn.model)} {suffix}</b><br>{plain(turn.assistant)}</p>')
            if turn.error:
                parts.append(f'<p>{plain(turn.error)}</p>')
            if turn.sources:
                links = '<br>'.join(
                    f'[{index}] <a href="{escape(source["url"], quote=True)}">{plain(source["title"])}</a>'
                    for index, source in enumerate(turn.sources, 1))
                parts.append(f'<p><b>Web検索の参照元</b><br>{links}</p>')
        self.transcript.setHtml(''.join(parts))
        scroll.setValue(scroll.maximum() if at_bottom else position)

    def new_conversation(self):
        if self._closed or self.service.is_running or self._connecting or self.store.read_failed:
            return
        if not self.flush() or not (self.store.turns or self.store.draft or self.store.active.draft_images):
            self.input.setFocus()
            return
        previous = self.store.active_id
        entry = ChatConversation()
        self.store.conversations.append(entry)
        self.store.active_id = entry.id
        if not self.store.save():
            self.store.conversations.pop()
            self.store.active_id = previous
            self.save_status.setText(self.store.error)
            return
        self.input.blockSignals(True)
        self.input.clear()
        self.input.blockSignals(False)
        self.attachments.set_images([])
        self._render_history()
        self._render()
        self._update_state()
        self.input.setFocus()

    def flush(self):
        self._save_timer.stop()
        if not self._dirty:
            return True
        # Keep the last valid endpoint while the user is still typing a URL.
        try:
            self.store.settings = self._settings()
        except ValueError:
            pass
        self.store.draft = self.input.toPlainText()
        if not self.store.save():
            self.save_status.setText(self.store.error)
            return False
        self._dirty = False
        self.save_status.setText('')
        return True

    def shutdown(self):
        self._closed = True
        self._startup_timer.stop()
        self._credit_timer.stop()
        self._save_timer.stop()
        self._render_timer.stop()
        self.account_service.shutdown()
        self.service.shutdown()

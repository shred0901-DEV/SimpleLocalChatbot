"""Chat image MIME handling and removable attachment previews."""
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QScrollArea, QToolButton, QWidget

from core.chat_images import MAX_PIXELS


def attachment_mime(source):
    return source.hasImage() or source.hasUrls()


def thumbnail(store, image):
    result = QImage.fromData(store.read(image.id))
    if result.isNull():
        raise ValueError('保存された画像のプレビューを読み込めません。')
    return result.scaled(200, 120, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)


def clipboard_bytes(image):
    if image.isNull() or image.width() * image.height() > MAX_PIXELS:
        raise ValueError('貼り付ける画像は2400万画素以下にしてください。')
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, 'PNG'):
        raise ValueError('クリップボードの画像を読み込めません。')
    return bytes(data)


class ChatMessageInput(QPlainTextEdit):
    filesReceived = Signal(object)
    imageReceived = Signal(QImage)
    attachmentFailed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.images_enabled = True

    def canInsertFromMimeData(self, source):
        return attachment_mime(source) or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source):
        if not attachment_mime(source):
            super().insertFromMimeData(source)
            return
        if self.isReadOnly() or not self.images_enabled:
            self.attachmentFailed.emit('画像の添付は応答やモデル解放が終了してから行ってください。')
            return
        urls = source.urls()
        if urls and all(url.isLocalFile() for url in urls):
            self.filesReceived.emit([url.toLocalFile() for url in urls])
        elif source.hasImage():
            value = source.imageData()
            image = value.toImage() if isinstance(value, QPixmap) else QImage(value)
            if image.isNull():
                self.attachmentFailed.emit('貼り付ける画像を読み込めません。')
            else:
                self.imageReceived.emit(image)
        else:
            # A copied URL remains text. Never fetch remote images implicitly.
            super().insertFromMimeData(source)

    def dragEnterEvent(self, event):
        if attachment_mime(event.mimeData()) and self.images_enabled and not self.isReadOnly():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if attachment_mime(event.mimeData()) and self.images_enabled and not self.isReadOnly():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if attachment_mime(event.mimeData()):
            self.insertFromMimeData(event.mimeData())
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class ChatAttachments(QScrollArea):
    removeRequested = Signal(int)

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFixedHeight(94)
        self.hide()

    def set_images(self, images):
        old = self.takeWidget()
        if old is not None:
            old.deleteLater()
        content = QWidget()
        row = QHBoxLayout(content)
        row.setContentsMargins(0, 0, 0, 0)
        for index, image in enumerate(images):
            label = QLabel()
            label.setToolTip(f'{image.name} ({image.width}×{image.height})')
            try:
                preview = thumbnail(self.store, image).scaled(
                    90, 64, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                label.setPixmap(QPixmap.fromImage(preview))
            except ValueError:
                label.setText('画像なし')
            row.addWidget(label)
            remove = QToolButton()
            remove.setText('×')
            remove.setToolTip('この画像を送信予定から外す')
            remove.clicked.connect(lambda _checked=False, index=index: self.removeRequested.emit(index))
            row.addWidget(remove)
        row.addStretch(1)
        self.setWidget(content)
        self.setVisible(bool(images))

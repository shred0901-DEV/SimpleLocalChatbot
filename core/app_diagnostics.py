"""Keep application logs and native crash tracebacks after the console closes."""
import faulthandler
import logging
import os
from datetime import datetime
from pathlib import Path

_handler = None
_qt_handler = None


def enable_diagnostics(directory):
    global _handler, _qt_handler
    if _handler is not None:
        return Path(_handler.baseFilename)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'app_{datetime.now():%Y%m%d_%H%M%S}_{os.getpid()}.log'
    handler = logging.FileHandler(path, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] [%(threadName)s] %(name)s: %(message)s'))
    logging.getLogger().addHandler(handler)
    _handler = handler  # Keep the file descriptor alive for faulthandler.
    faulthandler.enable(file=handler.stream, all_threads=True)
    from PySide6.QtCore import qInstallMessageHandler, QtMsgType

    def qt_message(kind, context, message):
        level = {QtMsgType.QtDebugMsg: logging.DEBUG, QtMsgType.QtInfoMsg: logging.INFO,
                 QtMsgType.QtWarningMsg: logging.WARNING, QtMsgType.QtCriticalMsg: logging.ERROR,
                 QtMsgType.QtFatalMsg: logging.CRITICAL}.get(kind, logging.WARNING)
        logging.getLogger('Qt').log(level, '%s', message)
        handler.flush()

    _qt_handler = qt_message
    qInstallMessageHandler(qt_message)
    logging.getLogger(__name__).info('診断ログ: %s', path)
    return path

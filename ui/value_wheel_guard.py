"""Require Ctrl for wheel changes to numeric inputs and closed combo boxes."""
from PySide6.QtCore import QObject, QEvent, Qt, QPointF
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QAbstractSpinBox, QDoubleSpinBox, QComboBox, QLineEdit, QAbstractScrollArea
from shiboken6 import isValid


class ValueWheelGuard(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._forwarding = False

    def eventFilter(self, watched, event):
        if self._forwarding or event.type() != QEvent.Type.Wheel:
            return False
        control = watched
        if isinstance(watched, QLineEdit):
            control = watched.parentWidget()
        if not isinstance(control, (QAbstractSpinBox, QComboBox)):
            return False
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            decimal_step = isinstance(control, QDoubleSpinBox) and control.decimals() in (1, 2)
            if decimal_step or isinstance(control, QAbstractSpinBox) and control.property('exactWheelStep'):
                step, step_type = control.singleStep(), control.stepType()
                # Qt normally multiplies Ctrl-wheel steps by ten. Remove Ctrl
                # from this forwarded event so each notch changes one unit of
                # the last displayed decimal (0.1 or 0.01).
                forwarded = QWheelEvent(QPointF(control.mapFromGlobal(event.globalPosition().toPoint())),
                    event.globalPosition(), event.pixelDelta(), event.angleDelta(), event.buttons(),
                    event.modifiers() & ~Qt.KeyboardModifier.ControlModifier, event.phase(), event.inverted())
                self._forwarding = True
                try:
                    control.setStepType(QAbstractSpinBox.StepType.DefaultStepType)
                    control.setSingleStep(10 ** -control.decimals() if decimal_step else step)
                    QApplication.sendEvent(control, forwarded)
                finally:
                    self._forwarding = False
                    if isValid(control):
                        control.setStepType(step_type)
                        control.setSingleStep(step)
                event.accept()
                return True
            return False
        # Let the containing settings area scroll instead of changing a value.
        parent = control.parentWidget()
        while parent is not None:
            if isinstance(parent, QAbstractScrollArea):
                viewport = parent.viewport()
                forwarded = QWheelEvent(QPointF(viewport.mapFromGlobal(event.globalPosition().toPoint())),
                    event.globalPosition(), event.pixelDelta(), event.angleDelta(), event.buttons(),
                    event.modifiers(), event.phase(), event.inverted())
                QApplication.sendEvent(viewport, forwarded)
                break
            parent = parent.parentWidget()
        event.accept()
        return True


def install_value_wheel_guard():
    app = QApplication.instance()
    if app is not None and not hasattr(app, '_value_wheel_guard'):
        app._value_wheel_guard = ValueWheelGuard(app)
        app.installEventFilter(app._value_wheel_guard)

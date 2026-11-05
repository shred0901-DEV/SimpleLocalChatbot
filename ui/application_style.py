"""Shared appearance for settings frames across all tabs and dialogs."""
from PySide6.QtWidgets import QApplication


GROUP_FRAME_STYLE = """
QGroupBox {
    border: 1px solid #ffffff;
    border-radius: 3px;
    margin-top: 0.7em;
    padding: 6px;
}
QGroupBox[title=""] {
    margin-top: 0px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0px 4px;
}
"""


SELECTED_BACKGROUND = '#99cfff'
SELECTED_FOREGROUND = '#202020'

# Native checked-button painting is not guaranteed to use the palette highlight.
# Both controls deliberately share these explicit colors on every OS/theme.
SELECTED_TAB_STYLE = f"""
QTabBar::tab:selected {{
    background-color: {SELECTED_BACKGROUND};
    color: {SELECTED_FOREGROUND};
}}
"""

COLLAPSE_BUTTON_STYLE = f"""
QToolButton[collapseToggle="true"] {{
    background-color: palette(button);
    color: palette(button-text);
    border: 1px solid palette(mid);
    border-radius: 3px;
    padding: 3px 6px;
}}
QToolButton[collapseToggle="true"]:checked {{
    background-color: {SELECTED_BACKGROUND};
    color: {SELECTED_FOREGROUND};
    border-color: #8abbe8;
}}
"""


def apply_application_style():
    app = QApplication.instance()
    if app is not None:
        style = app.styleSheet()
        for rules in (GROUP_FRAME_STYLE, SELECTED_TAB_STYLE, COLLAPSE_BUTTON_STYLE):
            if rules not in style:
                style += '\n' + rules
        if style != app.styleSheet():
            app.setStyleSheet(style)

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QDoubleValidator, QValidator
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
    CaptionLabel,
    LineEdit,
    SimpleCardWidget,
    Slider,
    StrongBodyLabel,
    SubtitleLabel,
    TransparentPushButton,
)


@dataclass(frozen=True, slots=True)
class ControlSpec:
    key: str
    title: str
    minimum: float
    maximum: float
    default: float
    step: float
    suffix: str = ""
    decimals: int = 2
    reference: str = "0"
    tooltip: str = ""


class SliderControl(QWidget):
    valueChanged = Signal(float)

    def __init__(self, spec: ControlSpec, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.spec = spec
        self.setToolTip(spec.tooltip or f"Adjust {spec.title}.")
        self._updating = False
        self._scale = max(1, round(1.0 / spec.step))
        # The control's value lives here, and the numeric field is a view of it. Reading
        # the field instead made the field authoritative, which it cannot be: the
        # validator refuses to let editingFinished fire for out-of-range text, so a typed
        # 5000 in a 0-100 control stayed on screen and value() handed 5000 back --
        # unclamped, unquantized, and describing neither the slider nor the state. What
        # then read it was the profile build and the pattern view.
        self._value = float(spec.default)

        title = StrongBodyLabel(spec.title, self)
        title.setToolTip(spec.tooltip)
        title.setMinimumWidth(150)

        def _fmt(value: float) -> str:
            return f"{value:.{spec.decimals}f}" if spec.decimals > 0 else f"{value:.0f}"

        self.reference_label = CaptionLabel(
            f"Range: {_fmt(spec.minimum)}{spec.suffix} to {_fmt(spec.maximum)}{spec.suffix}  ·  "
            f"Step: {_fmt(spec.step)}{spec.suffix}  ·  Default: {spec.reference}",
            self,
        )
        self.reference_label.setToolTip(spec.tooltip)

        title_box = QVBoxLayout()
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(1)
        title_box.addWidget(title)
        title_box.addWidget(self.reference_label)

        self.slider = Slider(Qt.Orientation.Horizontal, self)
        self.slider.setMinimum(round(spec.minimum * self._scale))
        self.slider.setMaximum(round(spec.maximum * self._scale))
        self.slider.setSingleStep(max(1, round(spec.step * self._scale)))
        self.slider.setPageStep(max(1, round(spec.step * self._scale * 10)))
        self.slider.setToolTip(spec.tooltip)
        self.slider.setMinimumWidth(150)
        self.slider.setMaximumWidth(360)
        self.slider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # Do not use qfluentwidgets' SpinBox here. Some Fluent themes paint the
        # native spin arrows on top of the line edit. A plain validated editable
        # field is visually stable and avoids all arrow-button theme conflicts.
        self.value_edit = LineEdit(self)
        self.value_edit.setFixedWidth(82)
        self.value_edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.value_edit.setClearButtonEnabled(False)
        self.value_edit.setToolTip(spec.tooltip)
        validator = QDoubleValidator(spec.minimum, spec.maximum, spec.decimals, self.value_edit)
        validator.setNotation(QDoubleValidator.Notation.StandardNotation)
        self.value_edit.setValidator(validator)
        self.value_edit.editingFinished.connect(self._edit_finished)

        # Fine calibration with the mouse wheel. Each wheel notch changes exactly
        # one declared step, both over the slider and over the numeric field.
        # This deliberately ignores QSlider's default page-style wheel behavior.
        self.slider.installEventFilter(self)
        self.value_edit.installEventFilter(self)

        self.suffix_label = CaptionLabel(spec.suffix, self)
        self.suffix_label.setToolTip(spec.tooltip or f"Unit for {spec.title}.")
        self.suffix_label.setVisible(bool(spec.suffix))

        self.reset_button = TransparentPushButton("Reset", self)
        self.reset_button.setFixedSize(62, 28)
        self.reset_button.setToolTip(f"Reset {spec.title} to {spec.default:g}{spec.suffix}")
        self.reset_button.clicked.connect(lambda: self.set_value(spec.default, emit=True))

        # A screen reader names a control from its accessible name, not its tooltip, and
        # none of these had one: the slider and the field were announced as nothing, and
        # every row's button as just "Reset".
        self.slider.setAccessibleName(spec.title)
        self.slider.setAccessibleDescription(spec.tooltip)
        self.value_edit.setAccessibleName(f"{spec.title} exact value")
        self.value_edit.setAccessibleDescription(spec.tooltip)
        self.reset_button.setAccessibleName(f"Reset {spec.title}")

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addLayout(title_box)
        row.addWidget(self.slider, 1)
        value_label = CaptionLabel("Exact Value", self)
        value_label.setToolTip("Enter an exact numeric value manually, or hover this control and use the mouse wheel for one-step calibration changes.")
        row.addWidget(value_label)
        row.addWidget(self.value_edit)
        row.addWidget(self.suffix_label)
        row.addWidget(self.reset_button)
        row.addStretch(1)

        self.slider.valueChanged.connect(self._slider_changed)
        self.set_value(spec.default, emit=False)

    def eventFilter(self, watched, event) -> bool:
        if watched is self.value_edit and event.type() == QEvent.Type.FocusOut:
            # Text the validator will not accept never reaches editingFinished, so
            # without this it just stays there, contradicting the slider beside it with
            # no indication which one is real. Only unacceptable text is rewritten:
            # this filter runs before QLineEdit's own focus-out handler, and rewriting
            # acceptable text here would discard the edit before editingFinished sees it.
            state, _, _ = self.value_edit.validator().validate(self.value_edit.text(), 0)
            if state != QValidator.State.Acceptable:
                self._set_edit_text(self._value)
        if watched in (self.slider, self.value_edit) and event.type() == QEvent.Type.Wheel:
            delta = event.angleDelta().y()
            if delta == 0:
                delta = event.pixelDelta().y()
            if delta:
                direction = 1.0 if delta > 0 else -1.0
                self.set_value(self.value() + direction * self.spec.step, emit=True)
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def _slider_changed(self, raw: int) -> None:
        if self._updating:
            return
        self._updating = True
        value = raw / self._scale
        self._value = value
        self._set_edit_text(value)
        self._updating = False
        self.valueChanged.emit(value)

    def _format_value(self, value: float) -> str:
        return f"{value:.{self.spec.decimals}f}" if self.spec.decimals > 0 else f"{value:.0f}"

    def _set_edit_text(self, value: float) -> None:
        self.value_edit.setText(self._format_value(value))

    def _edit_finished(self) -> None:
        if self._updating:
            return
        text = self.value_edit.text().strip().replace(",", ".")
        try:
            value = float(text)
        except ValueError:
            self._set_edit_text(self.value())
            return
        self.set_value(value, emit=True)

    def value(self) -> float:
        return self._value

    def set_value(self, value: float, emit: bool = False) -> None:
        bounded = min(self.spec.maximum, max(self.spec.minimum, float(value)))
        # Quantize to the declared step so the slider and editable numeric field agree.
        steps = round((bounded - self.spec.minimum) / self.spec.step)
        bounded = self.spec.minimum + steps * self.spec.step
        bounded = min(self.spec.maximum, max(self.spec.minimum, bounded))
        self._updating = True
        self._value = bounded
        self._set_edit_text(bounded)
        self.slider.setValue(round(bounded * self._scale))
        self._updating = False
        if emit:
            self.valueChanged.emit(bounded)


class Card(SimpleCardWidget):
    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setBorderRadius(10)
        self.setToolTip(subtitle or title)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 14, 16, 16)
        self.body.setSpacing(10)

        title_label = SubtitleLabel(title, self)
        title_label.setToolTip(subtitle or title)
        self.body.addWidget(title_label)
        if subtitle:
            subtitle_label = CaptionLabel(subtitle, self)
            subtitle_label.setToolTip(subtitle)
            subtitle_label.setWordWrap(True)
            self.body.addWidget(subtitle_label)

    def add_widget(self, widget: QWidget) -> None:
        self.body.addWidget(widget)

    def add_layout(self, layout: QHBoxLayout | QVBoxLayout) -> None:
        self.body.addLayout(layout)

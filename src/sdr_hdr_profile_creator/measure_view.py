"""The fullscreen surface a colorimeter reads, and the run that drives it.

Deliberately separate from ``pattern_view``. That window exists to be judged by
eye: it carries guidance text, adjustable controls, markers and a guided
sequence, all of which put light on screen and all of which a meter would
integrate along with the patch. Here the screen shows one patch on black and
nothing else.

Threading is the awkward part. A reading blocks for seconds -- spotread starts a
process, opens the instrument and integrates -- and Qt must keep presenting
frames meanwhile or Windows paints the window as not responding. So the sequence
runs on a worker thread while every frame is presented on the UI thread, with the
worker blocking until each patch is actually on screen. A patch reported as shown
before it has been presented would be read mid-transition.
"""

from __future__ import annotations

import time
from typing import Callable

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import QWidget

from vhdr_color import measure
from .edid import PanelMetadata
from .hdr_display import DisplayCapability, HdrDisplayError, HdrSurface
from vhdr_color.measure import Calibration, MeasurementStep
from .meter import Reading
from .pattern_view import context_for, dim_cursor
from vhdr_color.patterns import measurement_frame, placement_frame


class MeasureWindow(QWidget):
    """Blacked-out fullscreen output showing one measurement patch at a time."""

    #: Emitted as the window goes away, so a run in flight can be stopped.
    closed = Signal()
    #: Emitted once the meter is reported to be in place and the run may start.
    ready = Signal()

    def __init__(
        self,
        capability: DisplayCapability | None,
        sdr_white_nits: float,
        panel: PanelMetadata | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_PaintOnScreen, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setWindowTitle("Virtual HDR OSD - measuring")
        # The pointer sits on the screen being measured, so it must not glow.
        self.setCursor(dim_cursor())

        self._context = context_for(capability, sdr_white_nits, panel)
        self._surface: HdrSurface | None = None
        self._frame: bytes = b""
        self.failure = ""
        #: Whether the most recent patch actually reached the display.
        self.shown = False
        #: True while the placement target is up and the run has not started.
        self.placing = False

    def paintEngine(self):  # noqa: D102 - Qt must not paint into a D3D surface
        return None

    def device_size(self) -> tuple[int, int]:
        """Size in real pixels. Qt reports logical units, which on a scaled
        display are smaller, and a stretched frame is not the patch requested."""
        ratio = self.devicePixelRatioF() or 1.0
        return (max(1, round(self.width() * ratio)), max(1, round(self.height() * ratio)))

    def begin(self) -> bool:
        """Create the swapchain and go black. False if HDR is unavailable."""
        width, height = self.device_size()
        try:
            self._surface = HdrSurface(int(self.winId()), width, height)
        except HdrDisplayError as exc:
            self.failure = str(exc)
            return False
        self.show_patch(MeasurementStep("blank", "", (0.0, 0.0, 0.0), 0.0, 0.0))
        return True

    def show_placement_target(self, fraction: float = measure.PEAK_WINDOW_FRACTION) -> bool:
        """Draw where the meter goes, at the exact geometry the patches will use.

        Only safe before the run: this is the one moment anything other than a patch may
        be lit, because nothing is being read yet. "The centre of the screen" is what the
        briefing can say in words and it is not enough -- the patches cover a few percent
        of the screen *area*, so their edges matter, and an instrument overhanging one
        reads part black and returns a luminance nothing downstream can tell is wrong.

        ``fraction`` defaults to the smallest window any patch uses rather than the
        common one, so a meter placed on this target covers every patch in the run.
        """
        if self._surface is None:
            return False
        width, height = self.device_size()
        frame = placement_frame(width, height, self._context, fraction=fraction)
        try:
            self._surface.present(frame)
        except HdrDisplayError as exc:
            self.failure = str(exc)
            return False
        self.placing = True
        return True

    @Slot(object)
    def show_patch(self, step: MeasurementStep) -> None:
        """Present one patch. Must run on the UI thread.

        ``shown`` records whether the frame actually reached the display. A
        surface that has been closed silently swallowed every later patch, so
        the meter went on reading a black desktop and the readings looked like
        a very dark display rather than like nothing at all.
        """
        self.shown = False
        if self._surface is None:
            return
        width, height = self.device_size()
        self._frame = measurement_frame(
            width, height, step.rgb, step.nits, self._context,
            # Per patch, because peak is measured on a smaller window than the rest.
            fraction=step.window_fraction,
            colour_space=step.colour_space,
        )
        try:
            self._surface.present(self._frame)
        except HdrDisplayError as exc:
            self.failure = str(exc)
            return
        self.shown = True

    def resizeEvent(self, event):  # noqa: D102
        super().resizeEvent(event)
        if self._surface is None:
            return
        width, height = self.device_size()
        try:
            self._surface.resize(width, height)
        except HdrDisplayError as exc:
            self.failure = str(exc)

    def closeEvent(self, event):  # noqa: D102
        if self._surface is not None:
            self._surface.close()
            self._surface = None
        # Before the surface goes, tell anyone measuring against it to stop.
        # Without this the worker kept driving spotread through the remaining
        # patches with nothing on screen, reading each one off a black desktop,
        # and the run finished by adopting whatever that produced.
        self.closed.emit()
        self._frame = b""
        super().closeEvent(event)

    def keyPressEvent(self, event):  # noqa: D102
        # Escape is the only control once measuring; everything else is automatic. While
        # the placement target is up there is one more, because the run has to start
        # when the meter is actually on the glass rather than when a timer says so.
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        if self.placing and event.key() in (
            Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space
        ):
            self.placing = False
            self.ready.emit()


class _WindowDisplay(QObject):
    """Shows patches on a MeasureWindow from a worker thread.

    ``BlockingQueuedConnection`` is what makes this correct: the worker stops
    until the UI thread has actually presented the frame. Returning sooner would
    let the meter read a patch that is still on its way to the screen.

    The handoff is a ``Signal(object)`` rather than ``QMetaObject.invokeMethod``
    with ``Q_ARG(object, step)``. That call does not merely fail to marshal a
    plain Python object -- it raises ``qArgDataFromPyType: Unable to find a
    QMetaType for "object"`` on the very first patch, which the worker caught and
    reported as a failed measurement. On screen the window opened and shut again
    at once. A signal carries an arbitrary Python object with no registered
    meta-type, and keeps the blocking guarantee.
    """

    patch = Signal(object)

    def __init__(self, window: MeasureWindow) -> None:
        super().__init__()
        self._window = window
        self.patch.connect(
            window.show_patch, Qt.ConnectionType.BlockingQueuedConnection
        )

    def show(self, step: MeasurementStep) -> None:
        self.patch.emit(step)
        self.require_shown()

    def require_shown(self) -> None:
        """Stop the run unless the last patch actually reached the display.

        Separate from ``show`` so it can be tested: exercising ``show`` needs a
        second thread, because a BlockingQueuedConnection issued from the
        thread that would service it deadlocks rather than returning.

        The window may have gone -- Esc closes it mid-run -- or the surface may
        have refused the frame. Either way the meter would go on reading, and
        reading a patch that is not on screen produces a number, which is
        exactly what nothing downstream can tell from a measurement."""
        if not getattr(self._window, "shown", False):
            raise measure.Aborted()


class PlacementWatcher(QObject):
    """Polls the meter until it is looking at the green target, then says so.

    This is the difference between "press Enter when the meter is in place" and the
    meter simply noticing. The person doing it has both hands on an instrument held flat
    against a screen and cannot see the keyboard; asking them to find a key is asking
    them to move the thing they have just positioned.

    Polling an instrument is not free. Each read starts a spotread process, opens the
    device and integrates, so this waits between attempts rather than spinning, and
    gives up after a bounded number of them -- an unattended run that never sees the
    target must not sit there driving a USB device indefinitely.
    """

    #: Emitted once the instrument reports it can see the target.
    detected = Signal()
    #: Emitted when the attempt budget runs out, so the caller can offer a way forward.
    gave_up = Signal()

    #: 45 attempts two seconds apart: about a minute and a half when each read answers
    #: promptly, longer when reads run to spotread's timeout. Far longer than placing a meter
    #: takes and short enough not to look like a hang if the target is never found.
    MAX_ATTEMPTS = 45
    INTERVAL_SECONDS = 2.0

    def __init__(
        self,
        read: Callable[[], Reading],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__()
        self._read = read
        self._sleep = sleep
        self._abort = False
        #: How many reads it took, for the log.
        self.attempts = 0

    def cancel(self) -> None:
        self._abort = True

    @Slot()
    def run(self) -> None:
        for _ in range(self.MAX_ATTEMPTS):
            if self._abort:
                return
            self.attempts += 1
            try:
                reading = self._read()
            except Exception:
                # A meter that is unplugged, busy, or still warming up is not a reason
                # to abandon placement -- the next attempt may well succeed, and the
                # run has not started, so nothing is at stake in trying again.
                reading = None
            if reading is not None and measure.sees_placement_target(reading):
                if not self._abort:
                    self.detected.emit()
                return
            self._sleep(self.INTERVAL_SECONDS)
        if not self._abort:
            self.gave_up.emit()


class MeasurementWorker(QObject):
    """Runs the measurement sequence off the UI thread."""

    progress = Signal(str, int, int)
    reading = Signal(str, object)
    finished = Signal(object, str)

    def __init__(
        self,
        display: measure.Display,
        read: Callable[[], Reading],
        peak_nits: float,
        sleep: Callable[[float], None] = time.sleep,
        full: bool = True,
        probe: Callable[[], measure.DisplayState] | None = None,
        expected: measure.DisplayState | None = None,
        gamut: bool = False,
    ) -> None:
        super().__init__()
        # A display rather than a window: the worker has no business knowing about
        # Qt widgets, and taking one made every outcome below untestable, because
        # a BlockingQueuedConnection issued from the thread that would service it
        # deadlocks rather than returning.
        self._display = display
        self._read = read
        self._peak_nits = peak_nits
        # Runs on this worker's thread, between patches. The Windows calls behind it
        # are plain Win32 and carry no thread affinity; what must not happen is the
        # probe touching a widget, and the app builds it so it never does.
        self._probe = probe
        self._expected = expected
        # Injected so tests are not charged the panel's settling time; the real
        # delays exist for the display, not for the code.
        self._sleep = sleep
        # The long sweep by default; the short core-only plan is for exercising
        # the mechanics of a run without paying for the full sweep.
        self._full = full
        self._gamut = gamut
        self._abort = False

    def cancel(self) -> None:
        self._abort = True

    @Slot()
    def run(self) -> None:
        """Measure, and report either a Calibration or a message. Never both."""
        try:
            result: Calibration | None = measure.run(
                self._display,
                self._read,
                peak_nits=self._peak_nits,
                on_progress=lambda step, index, total: self.progress.emit(
                    step.label, index, total
                ),
                on_reading=lambda step, value: self.reading.emit(step.key, value),
                should_abort=lambda: self._abort,
                sleep=self._sleep,
                full=self._full,
                gamut=self._gamut,
                probe=self._probe,
                expected=self._expected,
            )
        except measure.Aborted:
            self.finished.emit(None, "")
        except measure.MeasurementError as exc:
            self.finished.emit(None, str(exc))
        except Exception as exc:  # noqa: BLE001 - a worker must never die silently
            self.finished.emit(None, f"Measurement failed: {exc}")
        else:
            self.finished.emit(result, "")


class SustainedWorker(QObject):
    """Runs :func:`measure.sustained` off the UI thread.

    Its own worker rather than a flag on MeasurementWorker: the two report different
    things, stop for different reasons, and the sweep's progress signal has no meaning
    for a measurement whose length is decided by the readings rather than by a plan.
    """

    #: Emitted per reading, so a slow settle looks like progress rather than a hang.
    reading = Signal(int, float)
    #: The result and a message. Never both, and never neither.
    finished = Signal(object, str)

    def __init__(
        self,
        display: measure.Display,
        read: Callable[[], Reading],
        peak_nits: float,
        sleep: Callable[[float], None] = time.sleep,
        probe: Callable[[], measure.DisplayState] | None = None,
        expected: measure.DisplayState | None = None,
    ) -> None:
        super().__init__()
        self._display = display
        self._read = read
        self._peak_nits = peak_nits
        self._sleep = sleep
        self._probe = probe
        self._expected = expected
        self._abort = False

    def cancel(self) -> None:
        self._abort = True

    @Slot()
    def run(self) -> None:
        try:
            result = measure.sustained(
                self._display,
                self._read,
                peak_nits=self._peak_nits,
                sleep=self._sleep,
                should_abort=lambda: self._abort,
                on_reading=lambda index, nits: self.reading.emit(index, nits),
                probe=self._probe,
                expected=self._expected,
            )
        except measure.Aborted:
            self.finished.emit(None, "")
        except measure.MeasurementError as exc:
            self.finished.emit(None, str(exc))
        except Exception as exc:  # noqa: BLE001 - a worker must never die silently
            self.finished.emit(None, f"Sustained measurement failed: {exc}")
        else:
            self.finished.emit(result, "")


def start_sustained(
    window: MeasureWindow,
    read: Callable[[], Reading],
    peak_nits: float,
    on_reading: Callable[[int, float], None],
    on_finished: Callable[[object, str], None],
    sleep: Callable[[float], None] = time.sleep,
    probe: Callable[[], measure.DisplayState] | None = None,
    expected: measure.DisplayState | None = None,
) -> tuple[QThread, SustainedWorker]:
    """Begin a sustained measurement, returning the thread and worker to keep alive.

    Wired exactly as :func:`start` is, and for the same reasons: Esc must reach the
    worker directly because its event loop is busy, and the thread has to be joined
    before the outcome is handed back or finalising it aborts the process.
    """
    thread = QThread()
    worker = SustainedWorker(
        _WindowDisplay(window), read, peak_nits, sleep=sleep, probe=probe, expected=expected
    )
    worker.moveToThread(thread)
    window.closed.connect(worker.cancel, Qt.ConnectionType.DirectConnection)
    thread.started.connect(worker.run)
    worker.reading.connect(on_reading)

    def _done(result, message):
        thread.quit()
        thread.wait()
        on_finished(result, message)

    worker.finished.connect(_done)
    thread.start()
    return thread, worker


def start(
    window: MeasureWindow,
    read: Callable[[], Reading],
    peak_nits: float,
    on_progress: Callable[[str, int, int], None],
    on_finished: Callable[[Calibration | None, str], None],
    on_reading: Callable[[str, Reading], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    full: bool = True,
    probe: Callable[[], measure.DisplayState] | None = None,
    expected: measure.DisplayState | None = None,
    gamut: bool = False,
) -> tuple[QThread, MeasurementWorker]:
    """Begin a run, returning the thread and worker so the caller can cancel.

    The caller keeps both alive: a QThread that goes out of scope while running
    takes the worker with it, and Qt warns about the destroyed thread rather than
    reporting anything useful about the measurement.
    """
    thread = QThread()
    # MeasurementWorker takes an injectable sleep so tests are not charged the panel's
    # settling time; start() used not to forward one, which put every end-to-end test
    # of this function back on the real delays and is why there were none.
    worker = MeasurementWorker(
        _WindowDisplay(window), read, peak_nits, sleep=sleep, full=full, probe=probe,
        expected=expected, gamut=gamut,
    )
    worker.moveToThread(thread)
    # "Esc cancels" is what the status line and the README both promise. Nothing
    # called cancel() before this, so the whole abort path -- measure.Aborted,
    # both should_abort guards, and the cancelled branch that reports "nothing
    # was changed" -- was unreachable in the shipped app.
    #
    # Direct, not the default queued. worker lives in `thread`, whose event loop is
    # occupied by run() for the entire measurement, so a queued cancel() is not
    # delivered until run() has already returned -- measured: _abort was still False
    # when observed from inside run(). The run did stop, but only because
    # require_shown() raises once the surface is gone, which meant both should_abort
    # guards were dead from the Esc path, one whole spotread integration still ran
    # against a closed surface, and Esc during the final step let the loop finish, so
    # the user who cancelled was told their channels failed to add up instead of that
    # nothing had changed.
    #
    # cancel() only assigns a bool, and CPython guarantees that much; it is exactly
    # the kind of write a cross-thread flag is for.
    window.closed.connect(worker.cancel, Qt.ConnectionType.DirectConnection)

    thread.started.connect(worker.run)
    worker.progress.connect(on_progress)
    if on_reading is not None:
        worker.reading.connect(on_reading)

    def _done(result, message):
        # Stop the thread and join it *before* handing the outcome back. The caller's
        # on_finished drops its only references to this thread and worker, and
        # finalising a QThread that has not yet left exec() is a fail-fast abort --
        # exit code 0xC0000409, no traceback, no Qt message. That took the app down at
        # the end of every completed measurement, after the numbers had been saved,
        # so it looked like the app vanishing rather than like a crash in measuring.
        #
        # Waiting here cannot deadlock: this runs on the UI thread, because a signal
        # connected to a plain Python callable is delivered through a receiver created
        # in the connecting thread, which makes it a queued connection. The worker has
        # already returned from run() and is idle in exec() by the time this arrives.
        thread.quit()
        thread.wait()
        on_finished(result, message)

    worker.finished.connect(_done)
    thread.start()
    return thread, worker

"""The fullscreen measurement surface and the run that drives it.

The swapchain needs real hardware, so the surface is faked and what is checked is
everything around it: that a frame is the size the swapchain demands, that it is
sized in device pixels rather than Qt's logical units, and that a run reports
exactly one outcome whatever happens inside it.
"""

from __future__ import annotations

import os
import struct
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    from sdr_hdr_profile_creator import measure_view
    from vhdr_color.measure import Calibration, MeasurementStep
    from sdr_hdr_profile_creator.meter import MeterError, Reading

    GUI_AVAILABLE = True
    GUI_IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover - environment without Qt
    GUI_AVAILABLE = False
    GUI_IMPORT_ERROR = str(exc)


def reading(Y, x, y):
    if y <= 0.0:
        return Reading(X=0.0, Y=Y, Z=0.0, x=x, y=y)
    return Reading(X=(x / y) * Y, Y=Y, Z=((1.0 - x - y) / y) * Y, x=x, y=y)


def _combine(*channels):
    """The white three channels add up to.

    Computed rather than written down, because validate() now checks that red
    plus green plus blue really is the measured white. A hand-picked white that
    looks plausible fails that check -- the first version here was out by 11% on
    Z -- and the failure would have been about the fixture, not the code.
    """
    X = sum(c.X for c in channels)
    Y = sum(c.Y for c in channels)
    Z = sum(c.Z for c in channels)
    total = X + Y + Z
    return Reading(X=X, Y=Y, Z=Z, x=X / total, y=Y / total)


# At the balance level, not peak: the channels have to add up to the white
# measured beside them, which they cannot do where the limiter is running.
_RED = reading(21.5, 0.674586, 0.314418)
_GREEN = reading(71.0, 0.269814, 0.685949)
_BLUE = reading(9.0, 0.151222, 0.060916)

GOOD_ORDER = [
    reading(0.0, 0.3130, 0.3290),          # black
    reading(454.25, 0.3127, 0.3290),       # peak white on the small window
    reading(205.0, 0.3127, 0.3290),        # the same drive on the measurement window
    _combine(_RED, _GREEN, _BLUE),         # reference white, below the limiter
    _RED,
    _GREEN,
    _BLUE,
]


class FakeSurface:
    def __init__(self, *_args, **_kwargs):
        self.frames = []
        self.closed = False
        self.size = (0, 0)
        #: Which thread actually reached the swapchain. Recorded here rather than in
        #: the caller, because a thread noted before the call under test says nothing
        #: about where the call ended up.
        self.presented_on = None

    def present(self, pixels, vsync=True):
        import threading

        self.presented_on = threading.get_ident()
        self.frames.append(pixels)

    def resize(self, width, height):
        self.size = (width, height)

    def close(self):
        self.closed = True


def hdr_capability():
    """An output reporting HDR with credible luminance.

    context_for falls back to a non-HDR, display-referred context when there is
    no capability at all, which clamps every patch to the SDR white level. That
    is right for an unknown display and wrong for measuring one."""
    from sdr_hdr_profile_creator.hdr_display import DisplayCapability

    return DisplayCapability(
        device_name=r"\\.\DISPLAY1",
        left=0, top=0, right=3840, bottom=2160,
        bits_per_color=10,
        # DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020
        color_space=12,
        min_nits=0.0, max_nits=1015.24, max_full_frame_nits=265.05,
        red_primary=(0.674586, 0.314418),
        green_primary=(0.269814, 0.685949),
        blue_primary=(0.151222, 0.060916),
        white_point=(0.3127, 0.3290),
    )


class FakeDisplay:
    def __init__(self):
        self.shown = []

    def show(self, step):
        self.shown.append(step.key)


@unittest.skipUnless(GUI_AVAILABLE, f"GUI dependencies unavailable: {GUI_IMPORT_ERROR}")
class MeasureWindowTests(unittest.TestCase):
    qt_app = None

    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def build(self, width=640, height=400, ratio=1.0):
        window = measure_view.MeasureWindow(hdr_capability(), 240.0, None)
        window.resize(width, height)
        surface = FakeSurface()
        window._surface = surface
        patcher = mock.patch.object(
            type(window), "devicePixelRatioF", lambda _self: ratio
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return window, surface

    def test_qt_never_paints_into_the_d3d_surface(self):
        window, _ = self.build()
        self.assertIsNone(window.paintEngine())

    def test_frames_are_sized_in_device_pixels_not_qt_units(self):
        """At 125% scaling a fullscreen widget reports a smaller size than its
        client area, and handing that to the swapchain stretches the frame."""
        window, _ = self.build(width=1000, height=800, ratio=1.25)
        self.assertEqual(window.device_size(), (1250, 1000))

    def test_a_presented_frame_is_exactly_what_the_swapchain_expects(self):
        window, surface = self.build(width=320, height=200)
        window.show_patch(MeasurementStep("white", "Peak white", (1.0, 1.0, 1.0), 800.0))
        self.assertEqual(len(surface.frames[-1]), 320 * 200 * 8)

    def test_the_patch_carries_the_luminance_it_was_asked_for(self):
        window, surface = self.build(width=320, height=200)
        window.show_patch(MeasurementStep("white", "Peak white", (1.0, 1.0, 1.0), 800.0))
        frame = surface.frames[-1]
        centre = struct.unpack_from("<4e", frame, ((200 // 2) * 320 + 320 // 2) * 8)
        self.assertAlmostEqual(centre[0] * 80.0, 800.0, places=1)

    def test_wide_gamut_step_reaches_the_surface_with_signed_scRGB(self):
        window, surface = self.build(width=20, height=20)
        self.addCleanup(window.close)
        window.show_patch(MeasurementStep('gamut-green-100', 'BT.2020 Green',
                                         (0, 1, 0), 100, colour_space='BT.2020'))
        rgb = struct.unpack_from('<4e', surface.frames[-1], (10 * 20 + 10) * 8)
        self.assertTrue(window.shown)
        self.assertLess(rgb[0], 0)
        self.assertGreater(rgb[1], 1)
        self.assertLess(rgb[2], 0)

    def test_the_screen_starts_black(self):
        """Nothing should be lit before the first patch is asked for.

        The centre is the only place this is visible. Sampling the top-left
        corner, as this did, tests nothing: measurement_frame always draws a
        black surround, so row 0 is black whatever patch is showing. Starting on
        a white patch instead would flash the meter and warm the panel before
        the black reading, which is measured first precisely to avoid that.
        """
        window, surface = self.build(width=320, height=200)
        with mock.patch.object(measure_view, "HdrSurface", lambda *a, **k: surface):
            self.assertTrue(window.begin())
        self.assertTrue(surface.frames)
        centre = struct.unpack_from(
            "<4e", surface.frames[0], ((200 // 2) * 320 + 320 // 2) * 8
        )
        self.assertEqual(centre[:3], (0.0, 0.0, 0.0))

    def test_a_surface_that_cannot_be_created_is_reported_not_raised(self):
        window, _ = self.build()
        window._surface = None
        with mock.patch.object(
            measure_view, "HdrSurface",
            side_effect=measure_view.HdrDisplayError("no HDR"),
        ):
            self.assertFalse(window.begin())
        self.assertIn("no HDR", window.failure)

    def test_closing_releases_the_surface(self):
        """A surface left open holds the swapchain, and the next run cannot
        create one for the same window."""
        from PySide6.QtGui import QCloseEvent

        window, surface = self.build()
        window.closeEvent(QCloseEvent())
        self.assertTrue(surface.closed)


@unittest.skipUnless(GUI_AVAILABLE, f"GUI dependencies unavailable: {GUI_IMPORT_ERROR}")
class MeasurementWorkerTests(unittest.TestCase):
    """Exactly one outcome, whatever happens inside the run."""

    qt_app = None

    def test_wide_gamut_request_reaches_measurement_engine(self):
        worker = measure_view.MeasurementWorker(FakeDisplay(), lambda: GOOD_ORDER[0],
                                               1000, gamut=True)
        with mock.patch.object(measure_view.measure, 'run', return_value=None) as run:
            worker.run()
        self.assertTrue(run.call_args.kwargs['gamut'])

    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def run_worker(self, reader, peak_nits=1015.24):
        display = FakeDisplay()
        # Real settling delays would charge these tests 24 seconds for nothing.
        worker = measure_view.MeasurementWorker(
            display, reader, peak_nits, sleep=lambda _seconds: None,
            # These exercise the mechanics of a run, not the sweep, so the six-patch
            # plan is enough and sixty-nine readings would only slow them down.
            full=False,
        )
        outcomes = []
        worker.finished.connect(lambda result, message: outcomes.append((result, message)))
        return worker, display, outcomes

    def test_a_display_that_changes_mid_run_reaches_the_outcome_as_a_message(self):
        """The probe is threaded through the worker to measure.run, and its refusal
        comes back the way every other refusal does: no calibration, one message."""
        order = iter(GOOD_ORDER)
        samples = iter([{"profile": "A.icm"}] * 3 + [{"profile": "B.icm"}] * 99)
        worker = measure_view.MeasurementWorker(
            FakeDisplay(), lambda: next(order), 1015.24,
            sleep=lambda _seconds: None, full=False, probe=lambda: next(samples),
        )
        outcomes = []
        worker.finished.connect(lambda result, message: outcomes.append((result, message)))
        worker.run()
        self.assertEqual(len(outcomes), 1)
        result, message = outcomes[0]
        self.assertIsNone(result)
        self.assertIn("profile went from A.icm to B.icm", message)

    def test_the_expected_state_reaches_the_run_through_the_worker(self):
        order = iter(GOOD_ORDER)
        worker = measure_view.MeasurementWorker(
            FakeDisplay(), lambda: next(order), 1015.24,
            sleep=lambda _seconds: None, full=False,
            probe=lambda: {"mode": "SDR"}, expected={"mode": "HDR"},
        )
        outcomes = []
        worker.finished.connect(lambda result, message: outcomes.append((result, message)))
        worker.run()
        self.assertEqual(1, len(outcomes))
        self.assertIsNone(outcomes[0][0])
        self.assertIn("before the first patch", outcomes[0][1])

    def test_the_sustained_worker_forwards_the_probe_too(self):
        """A second worker with its own constructor and its own run(): a probe that
        only the sweep forwarded would leave the one measurement that lights every
        pixel unguarded."""
        samples = iter([{"mode": "HDR"}] * 2 + [{"mode": "SDR"}] * 99)
        worker = measure_view.SustainedWorker(
            FakeDisplay(), lambda: GOOD_ORDER[1], 1000.0,
            sleep=lambda _seconds: None, probe=lambda: next(samples),
        )
        outcomes = []
        worker.finished.connect(lambda result, message: outcomes.append((result, message)))
        worker.run()
        self.assertEqual(1, len(outcomes))
        self.assertIsNone(outcomes[0][0])
        self.assertIn("mode went from HDR to SDR", outcomes[0][1])

    def test_a_good_run_reports_a_calibration_and_no_message(self):
        order = iter(GOOD_ORDER)
        worker, display, outcomes = self.run_worker(lambda: next(order))
        worker.run()
        self.assertEqual(len(outcomes), 1)
        result, message = outcomes[0]
        self.assertIsInstance(result, Calibration)
        self.assertEqual(message, "")
        self.assertEqual(
            display.shown,
            ["black", "white", "window-white", "balance-white", "red", "green", "blue"],
        )

    def test_a_meter_failure_reports_a_message_and_no_calibration(self):
        def reader():
            raise MeterError("sensor in the wrong position")

        worker, _, outcomes = self.run_worker(reader)
        worker.run()
        result, message = outcomes[0]
        self.assertIsNone(result)
        self.assertIn("wrong position", message)

    def test_cancelling_reports_neither_a_calibration_nor_an_error(self):
        """A cancelled run is not a failure, and must not look like one."""
        order = iter(GOOD_ORDER)
        worker, _, outcomes = self.run_worker(lambda: next(order))
        worker.cancel()
        worker.run()
        result, message = outcomes[0]
        self.assertIsNone(result)
        self.assertEqual(message, "")

    def test_an_unexpected_error_still_produces_an_outcome(self):
        """A worker that dies silently leaves the window up with no explanation
        and no way to tell a hang from a crash."""
        def reader():
            raise ZeroDivisionError("something nobody predicted")

        worker, _, outcomes = self.run_worker(reader)
        worker.run()
        result, message = outcomes[0]
        self.assertIsNone(result)
        self.assertIn("Measurement failed", message)

    def test_progress_is_reported_for_every_patch(self):
        order = iter(GOOD_ORDER)
        worker, _, _ = self.run_worker(lambda: next(order))
        seen = []
        worker.progress.connect(lambda label, index, total: seen.append((label, index, total)))
        worker.run()
        self.assertEqual(len(seen), len(GOOD_ORDER))
        self.assertEqual(seen[0][2], len(GOOD_ORDER))
        self.assertEqual(seen[0][0], "Black level")


@unittest.skipUnless(GUI_AVAILABLE, f"GUI dependencies unavailable: {GUI_IMPORT_ERROR}")
class WindowDisplayTests(unittest.TestCase):
    """The one object that crosses threads, and the one that had no coverage.

    Every other test here injects a fake display, so nothing exercised the real
    handoff. The first version used QMetaObject.invokeMethod with
    Q_ARG(object, step), which does not merely fail to marshal a plain Python
    object -- it raises qArgDataFromPyType on the first patch. The worker caught
    that, reported a failed measurement, and the window opened and shut again at
    once, which is exactly what a user saw.
    """

    qt_app = None

    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PySide6.QtCore import QObject, QThread, Signal, Slot

        self.QThread = QThread
        self.window = measure_view.MeasureWindow(hdr_capability(), 240.0, None)
        self.window.resize(320, 200)
        self.surface = FakeSurface()
        self.window._surface = self.surface
        self.addCleanup(self.window.deleteLater)

        outcome = {}

        class Caller(QObject):
            done = Signal()

            def __init__(self, display, step):
                super().__init__()
                self._display = display
                self._step = step

            @Slot()
            def go(self):
                import threading

                outcome["thread"] = threading.get_ident()
                try:
                    self._display.show(self._step)
                    outcome["raised"] = None
                except Exception as exc:  # noqa: BLE001
                    outcome["raised"] = repr(exc)
                # Records whether the emit blocked until the frame was presented.
                outcome["presented_before_return"] = len(self.parent_frames()) > 0
                self.done.emit()

            def parent_frames(self):
                return outcome["frames_ref"]

        self.Caller = Caller
        self.outcome = outcome

    def run_from_worker(self, step):
        """Call display.show(step) on a real worker thread and drain the UI loop."""
        import threading

        display = measure_view._WindowDisplay(self.window)
        self.outcome["frames_ref"] = self.surface.frames
        self.ui_thread = threading.get_ident()

        thread = self.QThread()
        caller = self.Caller(display, step)
        caller.moveToThread(thread)
        thread.started.connect(caller.go)
        caller.done.connect(thread.quit)
        thread.start()

        for _ in range(600):
            self.qt_app.processEvents()
            if thread.isFinished():
                break
            self.QThread.msleep(5)
        thread.wait(3000)
        return display

    def test_a_patch_emitted_from_a_worker_thread_reaches_the_window(self):
        step = MeasurementStep("white", "Peak white", (1.0, 1.0, 1.0), 800.0)
        self.run_from_worker(step)
        self.assertIsNone(self.outcome["raised"], self.outcome["raised"])
        self.assertEqual(len(self.surface.frames), 1)

    def test_the_frame_carries_the_luminance_the_step_asked_for(self):
        """Proves the step survived the thread boundary intact rather than
        arriving as something Qt could marshal but not represent."""
        step = MeasurementStep("white", "Peak white", (1.0, 1.0, 1.0), 800.0)
        self.run_from_worker(step)
        frame = self.surface.frames[-1]
        centre = struct.unpack_from("<4e", frame, ((200 // 2) * 320 + 320 // 2) * 8)
        self.assertAlmostEqual(centre[0] * 80.0, 800.0, places=1)

    def test_the_frame_is_presented_before_show_returns(self):
        """A patch reported as shown before it is on screen would be read
        mid-transition, which is the whole reason this connection blocks."""
        step = MeasurementStep("white", "Peak white", (1.0, 1.0, 1.0), 800.0)
        self.run_from_worker(step)
        self.assertTrue(self.outcome["presented_before_return"])

    def test_the_frame_is_built_on_the_ui_thread(self):
        """Presenting from a worker thread is not allowed: the swapchain belongs to the
        thread that created it.

        The thread recorded in the worker was recorded *before* calling the code under
        test, so this asserted a property of its own moveToThread and nothing else.
        Switching the connection to DirectConnection ran show_patch on five distinct
        worker threads and all 26 tests still passed. What matters is which thread
        reaches the surface, so that is what is now recorded -- inside present().
        """
        step = MeasurementStep("black", "Black level", (0.0, 0.0, 0.0), 0.0)
        self.run_from_worker(step)
        self.assertNotEqual(self.outcome["thread"], self.ui_thread,
                            "the worker did not run on its own thread")
        self.assertEqual(len(self.surface.frames), 1)
        self.assertEqual(
            self.ui_thread, self.surface.presented_on,
            "the swapchain was driven from a thread that does not own it",
        )


@unittest.skipUnless(GUI_AVAILABLE, f"GUI dependencies unavailable: {GUI_IMPORT_ERROR}")
class CancellationTests(unittest.TestCase):
    """Esc is promised by the status line and the README; it has to work.

    Nothing called MeasurementWorker.cancel(), so the whole abort path was
    unreachable in the shipped app: measure.Aborted, both should_abort guards,
    and the branch reporting "nothing was changed". Pressing Esc closed the
    window and the worker carried on driving spotread through the remaining
    patches, reading each off a black desktop, then adopted the result.
    """

    qt_app = None

    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def window(self):
        window = measure_view.MeasureWindow(hdr_capability(), 240.0, None)
        window.resize(320, 200)
        window._surface = FakeSurface()
        self.addCleanup(window.deleteLater)
        return window

    def test_closing_the_window_announces_it(self):
        from PySide6.QtGui import QCloseEvent

        window = self.window()
        seen = []
        window.closed.connect(lambda: seen.append(True))
        window.closeEvent(QCloseEvent())
        self.assertEqual(seen, [True])

    def test_escape_closes_the_window(self):
        """Asserts that Escape calls close(), not that Qt then delivers a
        closeEvent: under the offscreen platform a widget that was never shown
        does not get one, which is Qt's behaviour rather than this app's. The
        real window is fullscreen and does."""
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent

        window = self.window()
        closed = []
        with mock.patch.object(type(window), "close", lambda _self: closed.append(True)):
            window.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                           Qt.KeyboardModifier.NoModifier))
        self.assertEqual(closed, [True])

    def test_an_unrelated_key_does_not_close_the_window(self):
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent

        window = self.window()
        closed = []
        with mock.patch.object(type(window), "close", lambda _self: closed.append(True)):
            window.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Space,
                                           Qt.KeyboardModifier.NoModifier))
        self.assertEqual(closed, [])

    def test_a_patch_that_reached_the_screen_is_marked_shown(self):
        window = self.window()
        window.show_patch(MeasurementStep("white", "Peak white", (1.0, 1.0, 1.0), 800.0))
        self.assertTrue(window.shown)

    def test_a_patch_with_no_surface_is_not_marked_shown(self):
        """It used to return silently, so every later patch was read off a black
        desktop and looked like a very dark display rather than like nothing."""
        window = self.window()
        window._surface = None
        window.show_patch(MeasurementStep("white", "Peak white", (1.0, 1.0, 1.0), 800.0))
        self.assertFalse(window.shown)

    def test_the_display_aborts_rather_than_letting_an_unshown_patch_be_read(self):
        """Exercised through require_shown rather than show, because a
        BlockingQueuedConnection issued from the thread that would service it
        deadlocks -- the hazard _WindowDisplay's own docstring describes."""
        from vhdr_color.measure import Aborted

        window = self.window()
        display = measure_view._WindowDisplay(window)

        window.shown = True
        display.require_shown()      # a patch that reached the screen: no complaint

        window.shown = False
        with self.assertRaises(Aborted):
            display.require_shown()

    def test_show_checks_that_the_patch_landed(self):
        """Asserted against the source: the emit cannot be exercised here."""
        import inspect

        source = inspect.getsource(measure_view._WindowDisplay.show)
        self.assertIn("require_shown", source)
    def test_closing_the_window_aborts_the_worker(self):
        """The wiring that was missing entirely.

        Built by hand rather than through start(), because start() launches a
        real QThread whose BlockingQueuedConnection waits on an event loop a
        unittest run does not provide -- which deadlocks rather than fails."""
        window = self.window()
        worker = measure_view.MeasurementWorker(
            FakeDisplay(), lambda: GOOD_ORDER[0], 1000.0, sleep=lambda _s: None,
            full=False,
        )
        window.closed.connect(worker.cancel)
        self.assertFalse(worker._abort)
        window.closed.emit()
        self.assertTrue(worker._abort)

    def test_closing_the_window_aborts_while_the_run_is_still_going(self):
        """Not just that cancel is wired, but that it lands in time to matter.

        worker lives in the thread whose event loop run() occupies for the whole
        measurement, so a *queued* cancel() is not delivered until run() has already
        returned: _abort was still False when read from inside run(). The run stopped
        anyway -- require_shown() raises once the surface is gone -- but both
        should_abort guards were dead from the Esc path, a full spotread integration
        still ran against a closed surface, and Esc during the final step let the loop
        finish, so a cancelled run reported that the channels failed to add up rather
        than that nothing had changed.

        The signal has to be emitted from the UI thread, which is where Esc comes from.
        Emitting it from the worker thread proves nothing: sender and receiver are then
        in the same thread and Qt calls the slot directly whatever the connection type.
        """
        import threading

        from PySide6.QtCore import QEventLoop, QObject, QTimer, Signal

        class Window(QObject):
            closed = Signal()

            def __init__(self):
                super().__init__()
                self.shown = True

            def show_patch(self, step):
                self.shown = True

        window = Window()
        loop = QEventLoop()
        reached_read = threading.Event()
        cancel_emitted = threading.Event()
        seen = {}
        holder = {}

        def read():
            # Worker thread. Hold here until the UI thread has emitted, then look at
            # the flag from inside the run -- which is the only place it matters.
            if not reached_read.is_set():
                reached_read.set()
                cancel_emitted.wait(timeout=10.0)
                seen["abort_inside_run"] = holder["worker"]._abort
            return GOOD_ORDER[0]

        def emit_from_ui_thread():
            if reached_read.wait(timeout=0.01):
                window.closed.emit()      # exactly what MeasureWindow.closeEvent does
                cancel_emitted.set()
            else:
                QTimer.singleShot(10, emit_from_ui_thread)

        thread, worker = measure_view.start(
            window, read, 1000.0,
            on_progress=lambda *_: None,
            on_finished=lambda *_: loop.quit(),
            sleep=lambda _s: None,
            full=False,
        )
        holder["worker"] = worker
        QTimer.singleShot(0, emit_from_ui_thread)
        QTimer.singleShot(20000, loop.quit)
        loop.exec()
        cancel_emitted.set()
        thread.quit()
        thread.wait(5000)

        self.assertIn("abort_inside_run", seen, "the worker never reached a reading")
        self.assertTrue(
            seen["abort_inside_run"],
            "cancel() had not landed while run() was still executing",
        )

    def test_an_aborted_run_reports_neither_a_result_nor_an_error(self):
        """A cancelled run is not a failure and must not look like one."""
        display = FakeDisplay()
        worker = measure_view.MeasurementWorker(
            display, lambda: GOOD_ORDER[0], 1000.0, sleep=lambda _s: None, full=False
        )
        outcomes = []
        worker.finished.connect(lambda r, m: outcomes.append((r, m)))
        worker.cancel()
        worker.run()
        self.assertEqual(outcomes, [(None, "")])
        self.assertEqual(display.shown, [])


@unittest.skipUnless(GUI_AVAILABLE, GUI_IMPORT_ERROR)
class ThreadLifetimeTests(unittest.TestCase):
    """A whole run through the real start(), with a real QThread and event loop.

    The rest of this file avoids that deliberately, and the cost of avoiding it was
    a crash nothing could see. start() used to hand the outcome to on_finished and
    only then call thread.quit(). The caller's on_finished is MainWindow's
    _measure_finished, which nulls _measure_window, _measure_thread and
    _measure_worker -- the only references there are. Finalising a QThread that has
    not yet left exec() is a fail-fast abort: exit code 0xC0000409, empty stderr, no
    Qt message and no traceback. It fired at the end of every completed measurement,
    after the readings had already been saved, so it presented as the app vanishing
    rather than as a failure in measuring, and every test here calls _measure_finished
    directly with no live thread.

    Running the loop for real is not the hazard the other classes' comments suggest:
    the deadlock they avoid comes from issuing a BlockingQueuedConnection from the
    thread that would have to service it, which is not what start() does.
    """

    qt_app = None

    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def run_one(self, **extra):
        """Drive a complete measurement and report what on_finished saw."""
        from PySide6.QtCore import QEventLoop, QObject, QThread, QTimer, Signal

        class Window(QObject):
            """Enough of MeasureWindow for start(): a closed signal and a patch sink."""

            closed = Signal()

            def __init__(self):
                super().__init__()
                self.shown = True

            def show_patch(self, step):
                self.shown = True

        loop = QEventLoop()
        seen = {}
        readings = iter(GOOD_ORDER)

        def on_finished(result, message):
            # Captured inside the callback: by the time it returns, the caller would
            # already have dropped its references.
            seen["running"] = holder["thread"].isRunning()
            seen["on_ui_thread"] = QThread.currentThread() is self.qt_app.thread()
            seen["message"] = message
            seen["result"] = result
            loop.quit()

        holder = {}
        thread, worker = measure_view.start(
            Window(),
            lambda: next(readings),
            1000.0,
            on_progress=lambda *_: None,
            on_finished=on_finished,
            sleep=lambda _seconds: None,
            full=False,
            **extra,
        )
        holder["thread"], holder["worker"] = thread, worker

        # Never hang the suite if the run never reports.
        QTimer.singleShot(30000, loop.quit)
        loop.exec()
        thread.quit()
        thread.wait(5000)
        return seen

    def test_the_thread_has_stopped_before_the_outcome_is_delivered(self):
        """The guard against the abort. If the thread is still running here, the
        caller is about to drop the last reference to it and take the app down."""
        seen = self.run_one()
        self.assertIn("running", seen, "the run never reported an outcome")
        self.assertFalse(
            seen["running"],
            "on_finished was handed the outcome while the worker thread was still "
            "running; dropping the last reference now is a fail-fast abort",
        )

    def test_the_outcome_arrives_on_the_ui_thread(self):
        """on_finished is MainWindow's, and it touches widgets and the status bar."""
        seen = self.run_one()
        self.assertTrue(seen.get("on_ui_thread"), "outcome delivered off the UI thread")

    def test_a_clean_run_reports_a_calibration_and_no_message(self):
        """Otherwise the two tests above could pass on a run that failed instantly."""
        seen = self.run_one()
        self.assertEqual("", seen.get("message"))
        self.assertIsInstance(seen.get("result"), Calibration)

    def test_an_expected_state_handed_to_start_is_checked(self):
        seen = self.run_one(probe=lambda: {"mode": "SDR"}, expected={"mode": "HDR"})
        self.assertIsNone(seen.get("result"))
        self.assertIn("before the first patch", seen.get("message", ""))

    def test_a_probe_handed_to_start_sustained_can_stop_the_run(self):
        """start_sustained is a separate function with its own worker construction;
        a probe it dropped would leave every real sustained run unguarded."""
        from PySide6.QtCore import QEventLoop, QObject, QTimer, Signal

        class Window(QObject):
            closed = Signal()

            def __init__(self):
                super().__init__()
                # require_shown() aborts the run unless the last patch reached the
                # display; without this the run ends with no result and no message
                # before the probe is ever consulted, and the test proves nothing.
                self.shown = True

            def show_patch(self, step):
                self.shown = True

        loop = QEventLoop()
        seen = {}

        def on_finished(result, message):
            seen["result"], seen["message"] = result, message
            loop.quit()

        # Baseline and the sample after reading 1 agree; the sample after reading 2
        # is the change, so the refusal names reading 2.
        samples = iter([{"mode": "HDR"}] * 2 + [{"mode": "SDR"}] * 99)
        thread, worker = measure_view.start_sustained(
            Window(), lambda: GOOD_ORDER[1], 1000.0,
            on_reading=lambda *_: None, on_finished=on_finished,
            sleep=lambda _seconds: None, probe=lambda: next(samples),
        )
        QTimer.singleShot(30000, loop.quit)
        loop.exec()
        thread.quit()
        thread.wait(5000)
        self.assertIsNone(seen.get("result"))
        self.assertIn("after reading 2", seen.get("message", ""))
        self.assertIn("mode went from HDR to SDR", seen.get("message", ""))

    def test_a_probe_handed_to_start_can_stop_the_run(self):
        """start() is the only way the app reaches the worker, so a probe that start()
        dropped on the floor would leave every real run unguarded while the unit
        tests of the worker stayed green."""
        samples = iter([{"mode": "HDR"}] * 2 + [{"mode": "SDR"}] * 99)
        seen = self.run_one(probe=lambda: next(samples))
        self.assertIsNone(seen.get("result"))
        self.assertIn("mode went from HDR to SDR", seen.get("message", ""))


@unittest.skipUnless(GUI_AVAILABLE, GUI_IMPORT_ERROR)
class EscapeReachabilityTests(unittest.TestCase):
    """Escape only cancels a run if the window is the one holding focus.

    MeasureWindow.keyPressEvent handles Escape and closeEvent emits `closed`, which
    start() connects to worker.cancel. All of that was wired and none of it could fire:
    the measure path showed the window fullscreen and never focused it, so Escape went
    to whatever had focus before -- the main window, underneath -- while the status line
    promised "Esc cancels" from behind the surface.

    Split deliberately into the two halves that are ours to get right. Whether a key
    press *routes* to a focused widget is Qt's business, and asserting it through
    QApplication.focusWidget() is not stable in a shared offscreen QApplication: that
    call reports focus within the active window, and whichever window another test left
    activated decides the answer. So the routing is not asserted here. That the app
    establishes the precondition is asserted in test_gui.MeasurementBriefingTests.
    """

    qt_app = None

    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def shown_window(self):
        window = measure_view.MeasureWindow(None, 240.0, None)
        self.addCleanup(window.deleteLater)
        self.addCleanup(window.close)
        window.showFullScreen()
        QApplication.processEvents()
        return window

    def test_escape_closes_the_window_and_announces_it(self):
        """The half that is ours: the key arrives, the run is told to stop."""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent

        window = self.shown_window()
        cancelled = []
        window.closed.connect(lambda: cancelled.append(True))
        window.keyPressEvent(
            QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        )
        QApplication.processEvents()
        self.assertTrue(cancelled, "Escape did not stop the run")

    def test_other_keys_do_not_stop_a_run(self):
        """A stray keystroke on a black screen must not discard a measurement."""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent

        window = self.shown_window()
        cancelled = []
        window.closed.connect(lambda: cancelled.append(True))
        for key in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_A):
            window.keyPressEvent(
                QKeyEvent(QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
            )
        QApplication.processEvents()
        self.assertEqual([], cancelled)

    def test_the_window_can_hold_focus(self):
        """The precondition the app now establishes. Without it Escape is delivered to
        whatever had focus before, and none of the above is ever reached."""
        from PySide6.QtCore import Qt

        window = self.shown_window()
        window.activateWindow()
        window.setFocus(Qt.FocusReason.OtherFocusReason)
        QApplication.processEvents()
        self.assertTrue(window.hasFocus(), "the measurement surface cannot take focus")


@unittest.skipUnless(GUI_AVAILABLE, GUI_IMPORT_ERROR)
class PlacementWatcherTests(unittest.TestCase):
    """The poll that lets the meter start the run by noticing the target.

    Polling an instrument is not free -- each read starts a spotread process, opens the
    device and integrates -- so what matters here is that it stops: on success, on
    cancel, and on running out of patience. An earlier version of this project once left
    spotread running for 200 seconds against a USB HID device, and that is the failure
    mode to design against.
    """

    qt_app = None

    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def reading(self, Y, x, y):
        return Reading(X=(x / y) * Y if y else 0.0, Y=Y, Z=0.0, x=x, y=y)

    def watcher(self, readings):
        """A watcher over a fixed script of readings, with the waits removed."""
        supplied = iter(readings)

        def read():
            try:
                return next(supplied)
            except StopIteration:
                return self.reading(0.0, 0.31, 0.33)

        watcher = measure_view.PlacementWatcher(read, sleep=lambda _s: None)
        seen = {"detected": 0, "gave_up": 0}
        watcher.detected.connect(lambda: seen.__setitem__("detected", seen["detected"] + 1))
        watcher.gave_up.connect(lambda: seen.__setitem__("gave_up", seen["gave_up"] + 1))
        return watcher, seen

    GREEN = (95.0, 0.24, 0.71)
    DARK = (0.0002, 0.31, 0.33)

    def test_it_stops_as_soon_as_the_target_is_seen(self):
        watcher, seen = self.watcher([self.reading(*self.DARK), self.reading(*self.GREEN)])
        watcher.run()
        self.assertEqual(1, seen["detected"])
        self.assertEqual(0, seen["gave_up"])
        self.assertEqual(2, watcher.attempts, "it kept reading after finding the target")

    def test_it_gives_up_rather_than_polling_for_ever(self):
        """An unattended run that never sees the target must not sit there driving the
        instrument indefinitely."""
        watcher, seen = self.watcher([])
        watcher.run()
        self.assertEqual(0, seen["detected"])
        self.assertEqual(1, seen["gave_up"])
        self.assertEqual(measure_view.PlacementWatcher.MAX_ATTEMPTS, watcher.attempts)

    def test_cancelling_stops_it_without_claiming_either_outcome(self):
        """Esc during placement. Neither signal should fire: nothing was detected and
        the wait did not run out, the user simply left."""
        watcher, seen = self.watcher([self.reading(*self.DARK)] * 5)
        watcher.cancel()
        watcher.run()
        self.assertEqual(0, seen["detected"])
        self.assertEqual(0, seen["gave_up"])
        self.assertEqual(0, watcher.attempts, "it read the instrument after being cancelled")

    def test_a_meter_that_errors_is_retried_rather_than_abandoned(self):
        """Unplugged, busy, or still warming up. The run has not started, so nothing is
        at stake in trying again -- and giving up here would be worse than waiting."""
        calls = {"n": 0}

        def read():
            calls["n"] += 1
            if calls["n"] < 3:
                raise MeterError("instrument is busy")
            return self.reading(*self.GREEN)

        watcher = measure_view.PlacementWatcher(read, sleep=lambda _s: None)
        seen = []
        watcher.detected.connect(lambda: seen.append(True))
        watcher.run()
        self.assertEqual([True], seen)
        self.assertEqual(3, calls["n"])

    def test_a_white_patch_does_not_count_as_placement(self):
        """The run itself shows white; confusing it with the target would let placement
        'succeed' from a stray reading rather than from the meter being on the glass."""
        watcher, seen = self.watcher([self.reading(100.0, 0.3127, 0.3290)] * 3)
        watcher.cancel()   # keep the test short; the point is what it did not emit
        watcher.run()
        self.assertEqual(0, seen["detected"])


if __name__ == "__main__":
    unittest.main()

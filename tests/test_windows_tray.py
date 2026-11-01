import threading
import unittest
from unittest.mock import Mock

from windows_tray import WindowsTray


class TrayLifecycleTests(unittest.TestCase):
    def make_tray(self):
        # Exercise lifecycle without creating a native icon on the test host.
        tray = WindowsTray.__new__(WindowsTray)
        tray.window = Mock()
        tray.icon = Mock()
        tray.on_error = Mock()
        tray.ready = threading.Event()
        tray.exiting = threading.Event()
        return tray

    def test_close_hides_without_destroying_or_stopping(self):
        tray = self.make_tray()
        tray.ready.set()
        self.assertTrue(tray.hide_on_close())
        tray.window.hide.assert_called_once()
        tray.window.destroy.assert_not_called()
        tray.icon.stop.assert_not_called()
        tray.show()
        tray.window.show.assert_called_once()
        tray.window.restore.assert_called_once()

    def test_exit_allows_window_to_close_and_stops_icon(self):
        tray = self.make_tray()
        tray.ready.set()
        tray.quit()
        tray.window.destroy.assert_called_once()
        self.assertFalse(tray.hide_on_close())
        tray.window.hide.assert_not_called()
        tray.stop()
        tray.icon.stop.assert_called_once()
        self.assertFalse(tray.ready.is_set())

    def test_no_tray_never_traps_window(self):
        tray = self.make_tray()
        self.assertFalse(tray.hide_on_close())
        tray.window.hide.assert_not_called()

    def test_backend_failure_restores_window_and_allows_exit(self):
        tray = self.make_tray()

        def fail(setup):
            setup(tray.icon)
            self.assertTrue(tray.hide_on_close())
            raise RuntimeError("Tray backend failed")

        tray.icon.run.side_effect = fail
        tray.run()
        tray.on_error.assert_called_once()
        tray.window.show.assert_called_once()
        self.assertFalse(tray.hide_on_close())


if __name__ == "__main__":
    unittest.main()

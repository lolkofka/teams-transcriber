"""Windows tray lifecycle, independent of the recording pipeline."""
import threading


class WindowsTray:
    def __init__(self, window, image_path, on_error):
        import pystray
        from PIL import Image

        self.window = window
        self.on_error = on_error
        self.ready = threading.Event()
        self.exiting = threading.Event()
        with Image.open(image_path) as image:
            tray_image = image.copy()
        self.icon = pystray.Icon(
            "TeamsTranscriber", tray_image, "Teams transcriber",
            menu=pystray.Menu(
                pystray.MenuItem("Открыть", self.show, default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Выход", self.quit),
            ),
        )

    def run(self):
        """Called by pywebview on its worker thread; Windows permits this."""
        def setup(icon):
            icon.visible = True
            self.ready.set()

        try:
            self.icon.run(setup=setup)
        except Exception:
            self.on_error()
            # Recover a hidden window if the tray backend fails.
            if not self.exiting.is_set():
                self.window.show()
        finally:
            self.ready.clear()

    def hide_on_close(self):
        # If the icon failed, allow normal exit rather than strand the process.
        if self.ready.is_set() and not self.exiting.is_set():
            self.window.hide()
            return True
        return False

    def show(self, icon=None, item=None):
        self.window.show()
        self.window.restore()

    def quit(self, icon=None, item=None):
        self.exiting.set()
        self.window.destroy()

    def stop(self):
        self.exiting.set()
        self.ready.clear()
        self.icon.stop()

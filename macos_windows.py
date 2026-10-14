"""Native window menu and shortcuts for the two transcriber windows."""
import sys
import threading

_menu_target = None


def install_window_menu(window, teams):
    if sys.platform != 'darwin':
        return
    from PyObjCTools import AppHelper

    def install():
        import AppKit
        from webview.platforms.cocoa import BrowserView
        global _menu_target

        class TranscriberWindowMenuTarget(AppKit.NSObject):
            def showTranscriber_(self, sender):
                native = BrowserView.instances[window.uid].window
                native.deminiaturize_(None)
                native.makeKeyAndOrderFront_(None)

            def showTeams_(self, sender):
                threading.Thread(target=teams.open, daemon=True).start()

            def nextWindow_(self, sender):
                app = BrowserView.app
                windows = [view.window for view in BrowserView.instances.values()
                           if view.window.isVisible() or view.window.isMiniaturized()]
                if windows:
                    current = app.keyWindow()
                    index = windows.index(current) if current in windows else -1
                    native = windows[(index + 1) % len(windows)]
                    native.deminiaturize_(None)
                    native.makeKeyAndOrderFront_(None)

        _menu_target = TranscriberWindowMenuTarget.alloc().init()
        app = BrowserView.app
        menu = AppKit.NSMenu.alloc().initWithTitle_('Окно')
        for title, action, key in (
            ('Транскрибер', 'showTranscriber:', '1'),
            ('Teams', 'showTeams:', '2'),
            ('Следующее окно', 'nextWindow:', '`'),
        ):
            item = menu.addItemWithTitle_action_keyEquivalent_(title, action, key)
            item.setTarget_(_menu_target)
            item.setKeyEquivalentModifierMask_(AppKit.NSEventModifierFlagCommand)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        parent = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_('Окно', None, '')
        parent.setSubmenu_(menu)
        app.mainMenu().addItem_(parent)
        app.setWindowsMenu_(menu)

    AppHelper.callAfter(install)

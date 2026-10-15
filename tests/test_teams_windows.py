import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from teams_auto import TeamsMeetingAutomation
from teams_windows import WindowsTeamsChat, WindowsTeamsControls, trusted_controls_source


class WindowsTeamsChatTests(unittest.TestCase):
    def test_windows_automation_selects_dom_controls(self):
        with patch('teams_auto.sys.platform', 'win32'):
            automation = TeamsMeetingAutomation(None, lambda: {}, lambda _: [], lambda _: None)
        self.assertIs(automation._controls_factory, WindowsTeamsControls)

    def test_only_trusted_https_frames_and_fresh_chat_are_used(self):
        bridge = WindowsTeamsChat()
        body = json.dumps(dict(kind='chat', frame='one', messages=[dict(author='Teacher', text='Task', datetime='2026-10-14T10:00:00Z')]))
        for origin in ('http://teams.microsoft.com', 'https://teams.microsoft.com.attacker.test', 'https://example.com', 'https://teams.microsoft.com:444'):
            bridge.receive(origin, body)
        self.assertEqual(bridge.chat_messages(), [])
        with patch('teams_windows.time.monotonic', return_value=100):
            bridge.receive('https://teams.microsoft.com/v2/', body)
            self.assertEqual(bridge.chat_messages()[0]['text'], 'Task')
        with patch('teams_windows.time.monotonic', return_value=104):
            self.assertEqual(bridge.chat_messages(), [])

    def test_malformed_and_oversized_messages_are_bounded(self):
        bridge = WindowsTeamsChat()
        for body in ('[]', '{}', 'invalid', 'x'*1500001, json.dumps(dict(kind='chat', frame='f', messages=[None, dict(text=1)]))):
            bridge.receive('https://teams.microsoft.com', body)
        self.assertEqual(bridge.chat_messages(), [])

    def test_outlook_controls_are_read_from_trusted_frame_only(self):
        bridge = WindowsTeamsChat()
        body = json.dumps(dict(kind='controls', frame='calendar', controls=[
            dict(id='1', name='Lesson, 8:30 — 10:00, Tuesday, October 06, 2026, Microsoft Teams Meeting',
                 automationId='event', checked=None),
            dict(id='2', name='Hidden', disabled=True)]))
        for source in ('http://outlook.office.com', 'https://outlook.office.com.attacker.test',
                       'https://outlook.office.com:444', 'https://example.com'):
            bridge.receive(source, body, object())
        bridge.receive('https://outlook.office.com/calendar', body)
        self.assertEqual(bridge.buttons(), [])
        with patch('teams_windows.time.monotonic', return_value=100):
            bridge.receive('https://outlook.office.com/calendar', body, object())
            self.assertEqual([button.id for button in bridge.buttons()], ['1'])
        with patch('teams_windows.time.monotonic', return_value=104):
            self.assertEqual(bridge.buttons(), [])
        self.assertTrue(trusted_controls_source('https://teams.microsoft.com'))

    def test_windows_controls_invoke_javascript_in_own_frame(self):
        bridge = WindowsTeamsChat()
        scripts = []

        class Frame:
            def ExecuteScriptAsync(self, script):
                scripts.append(script)
                return SimpleNamespace(Wait=lambda timeout: True, Result='true')

        bridge._browser = SimpleNamespace(with_core_webview2=lambda callback: callback(None))
        bridge.receive('https://outlook.office.com/calendar', json.dumps(dict(
            kind='controls', frame='calendar', controls=[dict(id='1', name='Join', automationId='primaryActionButton')])), Frame())
        controls = WindowsTeamsControls(SimpleNamespace(dom=bridge))
        button = controls.one(lambda name, item: name == 'Join')
        self.assertTrue(controls.invoke(button))
        self.assertIn('h.invoke("1","Join")', scripts[0])
        self.assertFalse(controls.invoke(None))


if __name__ == '__main__':
    unittest.main()

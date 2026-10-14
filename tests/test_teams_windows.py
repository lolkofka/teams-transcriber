import json
import unittest
from unittest.mock import patch

from teams_windows import WindowsTeamsChat


class WindowsTeamsChatTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()

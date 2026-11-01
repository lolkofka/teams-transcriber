import json
import base64
import time
import unittest
from unittest.mock import patch

from teams_windows import WindowsTeamsChat
from teams_audio import TeamsAudioTap


class WindowsTeamsChatTests(unittest.TestCase):
    def test_native_audio_bridge_routes_pcm_without_polluting_chat(self):
        tap = TeamsAudioTap()
        tap.installed.set()
        target = tap.subscribe()
        bridge = WindowsTeamsChat(tap)
        packet = json.dumps(dict(kind='pcm', frame='remote', rate=16000, time=time.time(),
                                 pcm=base64.b64encode(b'\x00\x20' * 1600).decode()))
        bridge.receive('https://attacker.test', packet)
        bridge.receive('https://teams.microsoft.com:444/', packet)
        self.assertTrue(target.empty())
        bridge.receive('https://teams.microsoft.com/v2/', packet)
        self.assertEqual(target.get_nowait()[3][0], .25)
        self.assertEqual(bridge.chat_messages(), [])

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

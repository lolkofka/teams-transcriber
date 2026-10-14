import datetime as dt
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from teams_macos import DOMButton, MacTeamsControls, MacTeamsDOM
from teams_auto import TeamsMeetingAutomation


class MacTeamsTests(unittest.TestCase):
    def test_calendar_tab_accepts_mac_keyboard_shortcut(self):
        for name in ('Calendar (⌃ ⇧ 5)', 'Календарь (⌃ ⇧ 5)', 'Calendar (Ctrl+5)', 'Календарь'):
            with self.subTest(name=name):
                button = DOMButton('f', '1', name, '')
                clicks = []
                controls = MacTeamsControls(SimpleNamespace())
                controls.buttons = lambda: [button]
                controls.invoke = lambda b: clicks.append(b)
                auto = TeamsMeetingAutomation(None, lambda: {}, lambda _: [], lambda _: None)
                auto._controls = controls
                with patch.object(auto._stop_event, 'wait'):
                    auto._ensure_calendar()
                self.assertEqual(clicks, [button])

    def test_untrusted_and_stale_frames_cannot_supply_controls(self):
        dom = MacTeamsDOM()
        body = json.dumps({'frame':'one','controls':[{'id':'1','name':'Join now','automationId':'join','checked':None}]})
        dom.receive('teams.microsoft.com.attacker.test',body,object())
        self.assertEqual(dom.buttons(), [])
        with patch('teams_macos.time.monotonic', return_value=100):
            dom.receive('outlook.office.com',body,object())
            self.assertEqual(dom.buttons()[0].Name, 'Join now')
        with patch('teams_macos.time.monotonic', return_value=104):
            self.assertEqual(dom.buttons(), [])
        dom.receive('teams.microsoft.com','[]',object())

    def test_device_state_requires_unique_control_and_explicit_state(self):
        controls = MacTeamsControls(SimpleNamespace())
        button = DOMButton('f','1','Camera','camera-button',False)
        self.assertEqual(controls.device_state('camera',[button]), (False,button))
        unknown = DOMButton('f','2','Microphone','microphone-button')
        self.assertEqual(controls.device_state('mic',[unknown]), (None,unknown))
        self.assertEqual(controls.device_state('camera',[button,button]), (None,None))
        # The label describes the action, not the current state.
        action = DOMButton('f','3','Включить микрофон','microphone-button',True)
        self.assertEqual(controls.device_state('mic',[action])[0], False)

    def test_prejoin_toggles_take_priority_over_device_option_buttons(self):
        controls = MacTeamsControls(SimpleNamespace())
        mic = DOMButton('f', '1', 'Mute mic (⇧ ⌘ M)', 'toggle-mute', True)
        camera = DOMButton('f', '2', 'Turn camera on (⇧ ⌘ O)', 'toggle-video', False)
        buttons = [mic, camera,
                   DOMButton('f', '3', 'Selected microphone, open microphone options', 'selected-microphone-display'),
                   DOMButton('f', '4', 'Open video options', 'video-flyout-open-button')]
        self.assertEqual(controls.device_state('mic', buttons), (True, mic))
        self.assertEqual(controls.device_state('camera', buttons), (False, camera))
        self.assertEqual(controls.device_state('mic', buttons + [mic]), (None, None))

    def test_mac_factory_is_selected_without_windows_dependencies(self):
        with patch('teams_auto.sys.platform','darwin'):
            auto = TeamsMeetingAutomation(None,lambda:{},lambda _:[],lambda _:None)
            self.assertIs(auto._controls_factory, MacTeamsControls)

    def test_participant_count_requires_an_explicit_number(self):
        controls = MacTeamsControls(SimpleNamespace())
        self.assertEqual(controls.participant_count([DOMButton('f','1','Participants 2','')]), 2)
        self.assertIsNone(controls.participant_count([DOMButton('f','1','Participants','')]))

    def test_unknown_device_state_prevents_final_join(self):
        clicks=[]
        controls=SimpleNamespace(device_state=lambda _: (None,None), invoke=lambda b:clicks.append(b))
        auto=TeamsMeetingAutomation(None,lambda:{},lambda _:[],lambda _:None)
        auto._controls=controls
        with self.assertRaises(RuntimeError):
            auto._safe_device_off('mic')
        self.assertEqual(clicks, [])

    def test_failed_join_is_retried_then_limited_to_three_attempts(self):
        now = dt.datetime.now()
        lesson = {'subject':'Test', 'start':now-dt.timedelta(seconds=10), 'end':now+dt.timedelta(hours=1)}
        controls = SimpleNamespace(buttons=lambda:[],in_call=lambda *_:False,
                                   events=lambda:[(dict(lesson), object())],one=lambda *_:None)
        auto = TeamsMeetingAutomation(SimpleNamespace(is_open=lambda:True),lambda:{'teams_auto_join':True},lambda _:[lesson],lambda _:None)
        auto._opened=True; auto._controls=controls
        key=auto._lesson_key(lesson)
        with patch.object(auto,'_join',side_effect=RuntimeError('prejoin not ready')) as join:
            for stamp in (100,131,162):
                with patch('teams_auto.time.monotonic',return_value=stamp):
                    with self.assertRaises(RuntimeError): auto._tick()
            self.assertEqual(join.call_count,3)
            self.assertIn(key,auto._attempted)


if __name__ == '__main__':
    unittest.main()

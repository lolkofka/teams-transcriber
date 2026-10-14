import datetime as dt
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from teams_auto import TeamsControls, control_label, match_lesson, parse_calendar_event, should_leave
from teams_browser import TeamsBrowser, teams_microphone_origin_allowed


class FakeButton:
    def __init__(self, name, automation_id):
        self.Name = name
        self.AutomationId = automation_id


class TeamsAutoTests(unittest.TestCase):
    def test_microphone_permission_is_scoped_to_secure_teams_page_and_frame(self):
        teams = 'https://teams.cloud.microsoft/'
        self.assertTrue(teams_microphone_origin_allowed(teams, teams, teams))
        self.assertTrue(teams_microphone_origin_allowed('https://teams.microsoft.com:443/', teams, teams))
        for other in ('http://teams.cloud.microsoft/', 'https://teams.cloud.microsoft:8443/',
                      'https://teams.cloud.microsoft.attacker.test/', 'https://outlook.office.com/', ''):
            for i in range(3):
                urls = [teams] * 3
                urls[i] = other
                self.assertFalse(teams_microphone_origin_allowed(*urls))

    def test_outlook_join_label_ignores_fluent_icons(self):
        self.assertEqual(control_label('\uf4fb\nJoin'), 'join')
        self.assertEqual(control_label('\uf4fbJoin'), 'join')
        self.assertEqual(control_label('Присоединиться сейчас'), 'присоединиться сейчас')
        self.assertEqual(control_label('Join Teams meeting'), 'join teams meeting')

    def setUp(self):
        self.event = parse_calendar_event(
            "Урок 9 триггеры практика, 8:30 — 10:00, Вторник, 06 октября, 2026, "
            "Microsoft Teams Meeting, Организатор: Группа")
        self.lesson = {
            "subject": "Триггеры",
            "start": dt.datetime(2026, 10, 6, 8, 30),
            "end": dt.datetime(2026, 10, 6, 10, 0),
        }

    def test_calendar_event_is_parsed(self):
        self.assertEqual(self.event["start"], self.lesson["start"])
        self.assertEqual(self.event["end"], self.lesson["end"])
        self.assertIsNone(parse_calendar_event(
            "Другое событие, 8:30 — 10:00, Вторник, 06 октября, 2026"))

    def test_english_calendar_event_is_parsed(self):
        event = parse_calendar_event(
            "Урок 5. Применение компьютеров (2)., 13:30 to 14:50, Tuesday, "
            "October 06, 2026, Microsoft Teams Meeting, By Группа, Tentative")
        self.assertEqual(event["start"], dt.datetime(2026, 10, 6, 13, 30))
        self.assertEqual(event["end"], dt.datetime(2026, 10, 6, 14, 50))

    def test_unique_invite_matches_journal_lesson(self):
        self.assertEqual(match_lesson(self.lesson, [(self.event, "button")]),
                         (self.event, "button"))
        self.assertIsNone(match_lesson(self.lesson, [(self.event, "a"), (self.event, "b")]))
        later = dict(self.lesson, start=dt.datetime(2026, 10, 6, 11, 30))
        self.assertIsNone(match_lesson(later, [(self.event, "button")]))

    def test_leave_needs_end_farewell_and_everyone_gone(self):
        end = self.lesson["end"]
        self.assertFalse(should_leave(end - dt.timedelta(seconds=1), end, 1.0, 1))
        self.assertFalse(should_leave(end, end, None, 1))
        self.assertFalse(should_leave(end, end, 1.0, 2))
        self.assertFalse(should_leave(end, end, 1.0, None))
        self.assertTrue(should_leave(end, end, 1.0, 1))

    def test_device_action_names_are_interpreted_without_toggling(self):
        controls = object.__new__(TeamsControls)
        self.assertEqual(controls.device_state("mic", [FakeButton(
            "Включить микрофон", "microphone-button")])[0], False)
        self.assertEqual(controls.device_state("mic", [FakeButton(
            "Выключить микрофон", "microphone-button")])[0], True)
        self.assertEqual(controls.device_state("camera", [FakeButton(
            "Turn off camera", "camera-button")])[0], True)
        self.assertEqual(controls.device_state("camera", [FakeButton(
            "Turn on camera", "camera-button")])[0], False)
        self.assertEqual(controls.device_state("camera", [FakeButton(
            "Turn camera on", "camera-button")])[0], False)
        self.assertEqual(controls.device_state("camera", [FakeButton(
            "Turn camera off", "camera-button")])[0], True)
        self.assertIsNone(controls.device_state("mic", [
            FakeButton("Включить микрофон", "microphone-button"),
            FakeButton("Включить микрофон", "microphone-button")])[0])

    def test_in_call_accepts_leave_button_with_shortcut(self):
        controls = object.__new__(TeamsControls)
        self.assertTrue(controls.in_call([FakeButton("Leave (Ctrl+Shift+H)", "")]))
        self.assertTrue(controls.in_call([FakeButton("Покинуть собрание", "")]))
        self.assertFalse(controls.in_call([FakeButton("Leave a comment", "")]))

    def test_closing_teams_resets_window_and_popup_handler(self):
        class ClosedEvent:
            callback = None

            def __iadd__(self, callback):
                self.callback = callback
                return self

        window = SimpleNamespace(events=SimpleNamespace(closed=ClosedEvent(), before_show=ClosedEvent()))
        browser = TeamsBrowser()
        with patch("teams_browser.webview.create_window", return_value=window):
            browser.open()
        browser._popup_handler = object()
        window.events.closed.callback()
        self.assertFalse(browser.is_open())
        self.assertIsNone(browser._popup_handler)


if __name__ == "__main__":
    unittest.main()

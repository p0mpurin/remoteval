import unittest
from unittest.mock import patch
import clean_agent as agent


class VisualTests(unittest.TestCase):
    def test_blank_and_red_rectangle_are_not_play(self):
        self.assertEqual(agent.play_banner_score(bytes(128*56*4)), 0)
        self.assertEqual(agent.play_banner_score(bytes((60, 50, 210, 255))*128*56), 0)

    def test_visual_lobby_without_presence_fields(self):
        with patch.object(agent.time, 'monotonic', return_value=100.):
            s = agent.StateStore()
            s.process(((42, 123),))
            s.observe('presence', {}, s.generation)
            s.observe('core', None, s.generation)
            s.observe('pregame', None, s.generation)
            self.assertFalse(s.snapshot()['ready_to_play'])
            s.verify_lobby(s.generation)
            status = s.snapshot()
            self.assertEqual(status['phase'], 'MENUS')
            self.assertEqual(status['readiness_basis'], 'visual')
            self.assertTrue(status['ready_to_play'])
            s.observe('core', 'match-1', s.generation)
            self.assertEqual(s.snapshot()['phase'], 'IN_GAME')
            self.assertFalse(s.snapshot()['ready_to_play'])

    def test_visual_does_not_override_queue_or_missing_remote_checks(self):
        with patch.object(agent.time, 'monotonic', return_value=100.):
            s = agent.StateStore()
            s.process(((42, 123),))
            s.verify_lobby(s.generation)
            self.assertFalse(s.snapshot()['ready_to_play'])
            s.observe('core', None, s.generation)
            s.observe('pregame', None, s.generation)
            s.observe('party', {'State': 'MATCHMAKING'}, s.generation)
            self.assertEqual(s.snapshot()['phase'], 'QUEUED')
            self.assertFalse(s.snapshot()['ready_to_play'])

    def test_visual_expiry(self):
        with patch.object(agent.time, 'monotonic', return_value=100.) as clock:
            s = agent.StateStore()
            s.process(((42, 123),))
            s.observe('core', None, s.generation)
            s.observe('pregame', None, s.generation)
            s.verify_lobby(s.generation)
            self.assertTrue(s.snapshot()['ready_to_play'])
            clock.return_value = 100.6
            self.assertFalse(s.snapshot()['ready_to_play'])

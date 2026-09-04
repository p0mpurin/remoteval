import datetime as dt
import unittest
from unittest.mock import patch

from clean_agent import StateStore, timestamp, Detector


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.clock = 100.
        self.mono = patch('clean_agent.time.monotonic', side_effect=lambda: self.clock)
        self.wall = patch('clean_agent.time.time', side_effect=lambda: 1700000000+self.clock)
        self.mono.start()
        self.wall.start()
        self.addCleanup(self.mono.stop)
        self.addCleanup(self.wall.stop)
        self.s = StateStore()
        self.s.process(((42, 123),))

    def put(self, source, data, generation=None, started=None):
        self.s.observe(source, data, self.s.generation if generation is None else generation, started)

    def presence(self, loop='MENUS', party='DEFAULT', **kw):
        self.put('presence', dict(sessionLoopState=loop, partyState=party, **kw))

    def test_stale_startup_menu_is_not_ready(self):
        self.presence()
        self.clock += .2
        self.presence()
        self.assertEqual(self.s.snapshot()['phase'], 'LOADING')
        self.assertFalse(self.s.snapshot()['ready_to_play'])

    def test_verified_lobby(self):
        self.presence()
        self.s.verify_lobby(self.s.generation)
        self.assertTrue(self.s.snapshot()['ready_to_play'])
        self.clock += .6
        self.assertFalse(self.s.snapshot()['ready_to_play'])

    def test_pregame_beats_queue_and_clears_timer(self):
        entry = dt.datetime.fromtimestamp(1700000000+80, dt.timezone.utc).isoformat()
        self.presence(party='MATCHMAKING', queueEntryTime=entry)
        self.assertEqual(self.s.snapshot()['queue_elapsed_secs'], 20)
        self.put('pregame', 'match-1')
        self.assertEqual(self.s.snapshot()['phase'], 'AGENT_SELECT')
        self.assertIsNone(self.s.snapshot()['queue_elapsed_secs'])
        self.presence(party='MATCHMAKING', queueEntryTime=entry)
        self.assertEqual(self.s.snapshot()['phase'], 'AGENT_SELECT')

    def test_core_beats_pregame_alert_is_retained(self):
        self.put('pregame', 'match-1')
        self.put('core', 'match-1')
        self.put('pregame', 'match-1')
        self.assertEqual(self.s.snapshot()['phase'], 'IN_GAME')
        self.assertEqual(self.s.snapshot()['alert']['id'], 1)
        self.s.process(None)
        self.assertEqual(self.s.snapshot()['phase'], 'OFFLINE')
        self.assertEqual(self.s.snapshot()['alert']['id'], 1)

    def test_restart_discards_old_responses(self):
        old = self.s.generation
        self.s.process(((43, 124),))
        self.put('core', 'old-match', old)
        self.assertEqual(self.s.snapshot()['phase'], 'LOADING')

    def test_errors_do_not_mean_absence(self):
        self.put('core', 'match-1')
        self.clock += 4
        self.s.process(self.s.identity)
        self.s.error('core', 'timeout')
        self.presence()
        self.assertEqual(self.s.snapshot()['phase'], 'IN_GAME')
        self.assertTrue(self.s.snapshot()['degraded'])

    def test_confirmed_return_to_menu(self):
        self.presence('PREGAME')
        self.put('core', 'match-1')
        self.clock += .1
        self.presence()
        self.put('core', None)
        self.put('pregame', None)
        self.assertEqual(self.s.snapshot()['phase'], 'MENUS')
        self.assertFalse(self.s.snapshot()['ready_to_play'])

    def test_no_invented_queue_clock(self):
        self.presence(party='MATCHMAKING')
        self.assertIsNone(self.s.snapshot()['queue_elapsed_secs'])

    def test_match_found_stops_clock_without_lock_permission(self):
        self.presence(party='MATCHMADE_GAME_STARTING')
        self.assertEqual(self.s.snapshot()['phase'], 'AGENT_SELECT')
        self.assertIsNone(self.s.snapshot()['pregame_id'])

    def test_request_order_and_old_absence(self):
        self.put('core', 'new', started=100)
        self.put('core', None, started=99)
        self.assertEqual(self.s.snapshot()['phase'], 'IN_GAME')

    def test_parsing(self):
        self.assertIsNone(timestamp('2026-01-01T00:00:00'))
        self.assertIsNotNone(timestamp('2026-01-01T00:00:00Z'))
        self.assertEqual(Detector._retry_after('5'), 5)


if __name__ == '__main__':
    unittest.main()

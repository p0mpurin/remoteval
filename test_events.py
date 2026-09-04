import base64
import json
import socket
import unittest
from unittest.mock import patch
import clean_agent as agent


def private(loop, party='DEFAULT', queue=None):
    return {'isValid': True, 'matchPresenceData': {'sessionLoopState': loop},
            'partyPresenceData': {'partyState': party, 'queueEntryTime': queue}}


class EventsTests(unittest.TestCase):
    def test_current_and_legacy_presence_schemas(self):
        s = agent.normalize_presence(private('MENUS'))
        self.assertEqual(s['sessionLoopState'], 'MENUS')
        self.assertEqual(s['partyState'], 'DEFAULT')
        self.assertEqual(s['schema'], 'nested')
        self.assertEqual(agent.normalize_presence({'sessionLoopState': 'PREGAME'})['schema'], 'flat')
        with self.assertRaises(ValueError):
            agent.normalize_presence({'isValid': False, 'sessionLoopState': 'MENUS'})
        with self.assertRaises(ValueError):
            agent.normalize_presence({'futureUnknownSchema': {}})

    def test_nested_queue_to_match_to_core_and_own_player_filter(self):
        with patch.object(agent.time, 'monotonic', return_value=100.):
            d = agent.Detector(agent.Config('eu','eu','test'))
            d.store.process(((42,123),))
            def emit(loop, party='DEFAULT', who='self'):
                row = {'puuid':who, 'product':'valorant', 'private':base64.b64encode(
                    json.dumps(private(loop,party,'2026-09-05T00:00:00Z')).encode()).decode()}
                d._handle_event(json.dumps([8,'OnJsonApiEvent_chat_v4_presences', {
                    'uri':'/chat/v4/presences', 'eventType':'Update', 'data':{'presences':[row]}}]),
                    'self',d.store.generation)
            emit('MENUS','MATCHMAKING')
            self.assertEqual(d.store.snapshot()['phase'],'QUEUED')
            emit('INGAME',who='friend')
            self.assertEqual(d.store.snapshot()['phase'],'QUEUED')
            emit('PREGAME')
            self.assertEqual(d.store.snapshot()['phase'],'AGENT_SELECT')
            self.assertIsNone(d.store.snapshot()['queue_elapsed_secs'])
            emit('INGAME')
            self.assertEqual(d.store.snapshot()['phase'],'IN_GAME')
            self.assertEqual(d.store.snapshot()['alert']['id'],1)
            emit('INGAME')
            self.assertEqual(d.store.snapshot()['alert']['id'],1)

    def test_rms_deletion_only_requests_revalidation(self):
        d = agent.Detector(agent.Config('eu','eu','test'))
        d.store.process(((42,123),))
        d._handle_event(json.dumps([8,'OnJsonApiEvent_riot-messaging-service_v1_message', {
            'uri':'/riot-messaging-service/v1/message/ares-core-game/core-game/v1/matches/test',
            'eventType':'Delete'}]), 'self',d.store.generation)
        self.assertTrue(d.remote_wake['core'].is_set())
        self.assertEqual(d.store.snapshot()['phase'],'LOADING')

    def test_websocket_fragmentation_and_ping(self):
        # Exercise real framing code with a socket pair, without Riot.
        left,right = socket.socketpair()
        left.settimeout(.02)
        ws = agent.RiotEvents.__new__(agent.RiotEvents)
        ws.sock,ws.buffer,ws.fragments,ws.fragmenting = left,bytearray(),bytearray(),False
        try:
            right.sendall(b'\x01\x03hel')  # incomplete fragmented text message
            self.assertIsNone(ws.receive())
            right.sendall(b'\x89\x01x\x80\x02lo')  # ping then final continuation
            self.assertEqual(ws.receive(),'hello')
            reply = right.recv(128)
            self.assertEqual(reply[0],0x8a)  # masked pong
            self.assertTrue(reply[1] & 128)
            right.sendall(b'\x81\x03one\x81\x03two')
            self.assertEqual(ws.receive(),'one')
            self.assertEqual(ws.receive(),'two')
        finally:
            ws.close()
            right.close()

    def test_unreal_queue_timestamp_and_sentinel(self):
        self.assertIsNone(agent.timestamp('0001.01.01-00.00.00'))
        self.assertEqual(agent.timestamp('2026.09.05-12.30.20'),agent.timestamp('2026-09-05T12:30:20Z'))

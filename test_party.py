"""Party integration checks using mocked HTTP only; never sends game commands."""
import copy
import time
import unittest
from unittest.mock import MagicMock, patch
import clean_agent as a

RAW = {'ID': 'party-one', 'State': 'DEFAULT', 'Accessibility': 'CLOSED',
       'Members': [{'Subject': 'me', 'IsOwner': True, 'IsReady': True},
                   {'Subject': 'friend', 'PlayerIdentity': {'Incognito': True}}],
       'EligibleQueues': ['unrated'], 'QueueIneligibilities': [], 'InviteCode': ''}

class PartyTests(unittest.TestCase):
    def setUp(self):
        self.d = a.Detector(a.Config('eu', 'eu', 'test'))
        self.d.auth = ('me', {'Authorization': 'test-only'}, 1, time.time()+100)
        self.party = a.sanitize_party(copy.deepcopy(RAW), 'me')
        self.snap = {'phase': 'MENUS', 'degraded': False, 'instance_id': 'instance',
                     'generation': 1, 'party': self.party}
        self.payload = {'instance': 'instance', 'generation': 1,
                        'party_id': 'party-one', 'account_id': 'me', 'action': 'leave'}
        self.d.store.snapshot = MagicMock(return_value=self.snap)
        self.detector_patch = patch.object(a, 'detector', self.d)
        self.detector_patch.start()
        self.addCleanup(self.detector_patch.stop)

    def http(self, detail=None):
        session = MagicMock()
        session.__enter__.return_value = session
        def reply(data):
            r = MagicMock(status_code=200, body=b'{}')
            r.json.return_value = data
            return r
        session.request.side_effect = [reply({'CurrentPartyID': 'party-one'}),
                                       reply(detail or RAW), reply({})]
        return session

    def test_membership_and_private_identity(self):
        self.assertTrue(self.party['leader'])
        self.assertFalse(a.sanitize_party(RAW, 'outsider')['available'])
        store = a.StateStore()
        store.process(((42,123),))
        store.observe('party_roster', self.party, store.generation)
        store.observe('names', {'friend': {'name': 'Hidden', 'tag': 'NO'}}, store.generation)
        result = store.snapshot()['party']
        self.assertEqual(result['members'][1]['name'], 'Party member')
        self.assertNotIn('subject', result['members'][1])
        self.assertNotIn('Hidden', str(result))
        store.sources['party_roster'] = (time.monotonic()-9, self.party)
        self.assertFalse(store.snapshot()['party']['available'])
        store.observe('party_roster', self.party, store.generation)
        store.process(None)
        self.assertFalse(store.snapshot()['party']['available'])

    def test_stale_account_party_or_generation_never_requests(self):
        with patch.object(a, '_HttpSession') as http:
            for key in ('instance','generation','party_id','account_id'):
                payload = dict(self.payload, **{key: 'different'})
                self.assertFalse(a.party_action(payload)['ok'])
            self.d.auth = ('different', {}, 2, time.time()+100)
            self.assertFalse(a.party_action(self.payload)['ok'])
            http.assert_not_called()

    def test_invite_escapes_path_and_wakes_refresh(self):
        session = self.http()
        with patch.object(a, '_HttpSession', return_value=session):
            result = a.party_action(dict(self.payload, action='invite', name='A B/?', tag='EU#1'))
        self.assertTrue(result['ok'])
        args = session.request.call_args_list[-1].args
        self.assertEqual(args[0], 'POST')
        self.assertTrue(args[1].endswith('/invites/name/A%20B%2F%3F/tag/EU%231'))
        self.assertTrue(self.d.remote_wake['party'].is_set())

    def test_changed_party_and_nonleader_never_mutate(self):
        session = self.http()
        session.request.side_effect = None
        session.request.return_value.status_code = 200
        session.request.return_value.body = b'{}'
        session.request.return_value.json.return_value = {'CurrentPartyID':'other'}
        with patch.object(a, '_HttpSession', return_value=session):
            self.assertFalse(a.party_action(self.payload)['ok'])
        self.assertEqual(session.request.call_count, 1)
        detail = copy.deepcopy(RAW)
        detail['Members'][0]['IsOwner'] = False
        session = self.http(detail)
        with patch.object(a, '_HttpSession', return_value=session):
            self.assertFalse(a.party_action(dict(self.payload, action='generate_code'))['ok'])
        self.assertEqual(session.request.call_count, 2)

    def test_queue_transition_and_invalid_code_never_mutate(self):
        for payload in (dict(self.payload, action='join_code', code='../unsafe'), self.payload):
            session = self.http()
            if payload is self.payload:
                self.d.store.snapshot.side_effect = [self.snap,dict(self.snap,phase='QUEUED')]
            with patch.object(a, '_HttpSession', return_value=session):
                self.assertFalse(a.party_action(payload)['ok'])
            self.assertEqual(session.request.call_count, 2)

if __name__ == '__main__':
    unittest.main()

"""Offline account-cache checks. No Riot or game commands."""
import time
import unittest
from unittest.mock import patch, MagicMock
import clean_agent as agent


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.d = agent.Detector(agent.Config('eu', 'eu', 'test'))
        self.d.auth = ('self', {}, 1, time.time()+100)
        self.d.dashboard_subject = 'self'

    def test_snapshot_unknown_zero_expiry_and_account_switch(self):
        self.assertFalse(self.d.dashboard_snapshot()['available'])
        self.d.dashboard_data = {'fetched_at': time.time(), 'balances': {'vp': 0}}
        self.assertEqual(self.d.dashboard_snapshot()['balances'], {'vp': 0})
        self.d.dashboard_data['fetched_at'] -= 100
        self.assertFalse(self.d.dashboard_snapshot()['available'])
        self.d.dashboard_data['fetched_at'] = time.time()
        self.d.auth = ('other', {}, 2, time.time()+100)
        self.assertFalse(self.d.dashboard_snapshot()['available'])
        self.d.auth = ('self', {}, 1, time.time()-1)
        self.assertFalse(self.d.dashboard_snapshot()['available'])

    def test_wallet_validation_and_inventory_cache(self):
        session = MagicMock()
        wallet = MagicMock(status_code=200)
        wallet.__enter__.return_value = wallet
        wallet.json.return_value = {'Balances': {
            '85ad13f7-3d1b-5128-9eb2-7cd8ee0b5741': 0,
            'e59aa87c-4cbf-517a-5983-6e81511be9b7': True,
            '85ca954a-41f2-ce94-9b45-8ca3dd39a00d': -3}}
        inventory = MagicMock(status_code=200)
        inventory.__enter__.return_value = inventory
        inventory.json.return_value = {'Entitlements': [{'ItemID': 'agent-id'}]}
        session.get.side_effect = [wallet, inventory]
        session.__enter__.return_value = session
        with patch.object(self.d, '_session', return_value=session), \
             patch.object(self.d.stop_event, 'wait', side_effect=[False, True]):
            self.d._dashboard_loop()
        result = self.d.dashboard_snapshot()
        self.assertEqual(result['balances'], {'vp': 0})
        self.assertEqual(result['owned_agents'], ['agent-id'])
        self.assertTrue(result['ownership_available'])


if __name__ == '__main__':
    unittest.main()

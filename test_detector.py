"""Offline tests; never connects to Riot or sends game commands."""
import http.client
import http.server
import json
import threading
import unittest
from unittest.mock import patch

import clean_agent as agent


class BundledTests(unittest.TestCase):
    def test_http_adapter_keepalive_and_errors(self):
        class FakeRiot(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_GET(self):
                code = 429 if self.path == '/limited' else 200
                body = b'{"MatchID":"example"}'
                self.send_response(code)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Retry-After', '7')
                self.end_headers()
                self.wfile.write(body)

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), FakeRiot)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = 'http://127.0.0.1:%d' % server.server_port
            with agent._HttpSession() as session:
                self.assertEqual(session.get(base+'/ok').json()['MatchID'], 'example')
                connection = session.connection
                response = session.get(base+'/limited')
                self.assertIs(connection, session.connection)
                self.assertEqual(response.headers.get('Retry-After'), '7')
                with self.assertRaises(agent._NetworkError):
                    response.raise_for_status()
                with self.assertRaises(ValueError):
                    session.get('https://example.com', verify=False)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_status_has_no_upstream_calls(self):
        detector = agent.Detector(agent.Config('eu', 'eu', 'test-version'))
        detector.store.process(None)
        with patch.object(agent, 'detector', detector), \
             patch.object(agent, 'glz', side_effect=AssertionError('upstream call')), \
             patch.object(agent, 'get_window_info', side_effect=AssertionError('window scan')):
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), agent.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
                conn.request('GET', '/status')
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                data = json.loads(response.read())
                self.assertEqual(data['phase'], 'OFFLINE')
                self.assertIn('window', data)
                conn.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()

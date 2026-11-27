"""The upstream decides each new request after a credential rejection."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from agy_bridge.application import Application
from agy_bridge.core import BridgeError, upstream_error


class AuthenticationOutcome(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Mock(root=Path(tmp.name))
        store.read.return_value = {'accounts': {'test': {}}}
        store._ready.return_value = True
        self.app = Application(Path(tmp.name) / 'native', store)

    def test_authentication_error_reaches_caller_but_does_not_lock_next_request(self):
        for raw in (
            {'error': {'code': 401, 'status': 'UNAUTHENTICATED', 'message': 'Authentication required'}},
            {'error': {'status': 'UNAUTHENTICATED', 'message': 'Authentication required'}},
            'Eligibility check failed: Error 401, Message: Authentication required, Status: UNAUTHENTICATED',
        ):
            with self.subTest(raw=raw):
                err = upstream_error(raw)
                self.assertEqual((err.status, err.code), (401, 'upstream_unauthenticated'))
                self.assertIn('Authentication required', err.public()['error']['message'])
                self.app.outcome('test', err, 'gemini-4-carbon-medium')
                self.assertEqual(self.app.health(), {})
                self.app.check_account('test', 'gemini-4-carbon-medium')

    def test_real_refusal_is_not_erased_by_later_auth_error(self):
        for error, expected in (
            ({'code': 403, 'status': 'PERMISSION_DENIED'}, 'account_permission_denied'),
            ({'code': 403, 'reason': 'TOS_VIOLATION'}, 'service_disabled'),
            ('Eligibility check failed: Service not currently available in your location', 'account_permission_denied'),
            ({'code': 429, 'status': 'RESOURCE_EXHAUSTED'}, 'rate_limited'),
        ):
            with self.subTest(expected=expected):
                alias = 'test' + expected + str(type(error).__name__)
                self.app.outcome(alias, upstream_error(error), 'gemini-4-carbon-medium')
                before = self.app.health()
                self.app.outcome(alias, upstream_error({'code': 401}), 'gemini-4-carbon-medium')
                self.assertEqual(before, self.app.health())
                with self.assertRaises(BridgeError) as ctx:
                    self.app.check_account(alias, 'gemini-4-carbon-medium')
                self.assertEqual(ctx.exception.code, expected)

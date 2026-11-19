"""A foreground sample belongs to one request on one unlocked console."""
import json
import subprocess
import unittest
import uuid
from unittest.mock import patch

import winhoo_macos as desktop


class ForegroundTests(unittest.TestCase):
    def setUp(self):
        self.state = {'uuid': str(uuid.uuid4()), 'account': 'TestUser'}
        self.request = uuid.uuid4()
        self.sample = {
            'owner': self.state['uuid'], 'request': self.request.hex,
            'account': 'TESTUSER', 'computer': 'TEST',
            'consoleSession': {'account': r'TEST\TestUser', 'id': 1, 'connection': 'active', 'lock': 'unlocked'},
            'console': r'TEST\TestUser', 'session': 1, 'pid': 37, 'process': 'explorer',
        }

    def observe(self, sample, expected=None, **kwargs):
        reply = subprocess.CompletedProcess([], 0, json.dumps(sample), '')
        with patch.object(desktop, 'guest', return_value=reply), patch.object(desktop.uuid, 'uuid4', return_value=self.request):
            return desktop.foreground(None, self.state, expected, **kwargs)

    def test_focus_requires_proof_of_activation_from_the_requested_process(self):
        for method in ('already-active','caption-click'):
            sample = {**self.sample,'method':method}
            self.assertEqual(self.observe(sample,37,activate=True),sample)
            with self.assertRaisesRegex(ValueError,'expected PID 42'):
                self.observe(sample,42,activate=True)
        with self.assertRaisesRegex(ValueError,'did not confirm'):
            self.observe(self.sample,37,activate=True)
        for pid,ticks in ((None,None),(-1,None),(37,0)):
            with self.subTest(pid=pid,ticks=ticks), self.assertRaises(ValueError):
                self.observe(self.sample,pid,activate=True,started_ticks=ticks)

    def test_rejects_explorer_when_the_browser_was_expected(self):
        self.assertEqual(self.observe(self.sample, 37), self.sample)
        with self.assertRaisesRegex(ValueError, 'explorer.*37.*42'):
            self.observe(self.sample, 42)

    def test_rejects_stale_foreign_locked_or_noninteractive_sample(self):
        for change in (
            {'request': uuid.uuid4().hex}, {'owner': str(uuid.uuid4())},
            {'account': 'Other'}, {'session': 0}, {'session': 2}, {'pid': 0},
            {'consoleSession': {**self.sample['consoleSession'], 'lock': 'locked'}},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.observe({**self.sample, **change})

    def test_transport_timeout_does_not_return_an_earlier_window(self):
        with patch.object(desktop, 'guest', side_effect=subprocess.TimeoutExpired('ssh', 45)) as probe:
            with self.assertRaisesRegex(ValueError, 'no window was confirmed'):
                desktop.foreground(None, self.state)
        probe.assert_called_once()

    def test_timeout_identifies_the_last_phase_of_only_this_request(self):
        other = uuid.uuid4().hex
        stderr = (f'winhoo-foreground:{self.request.hex}:dispatch\n'
                  f'winhoo-foreground:{self.request.hex}:await-sample\n'
                  f'winhoo-foreground:{other}:cleanup\n').encode()
        timeout = subprocess.TimeoutExpired('ssh', 85, output=b'{"pid":37}', stderr=stderr)
        with patch.object(desktop.uuid, 'uuid4', return_value=self.request), \
             patch.object(desktop, 'guest', side_effect=timeout) as probe:
            with self.assertRaisesRegex(ValueError, '85 seconds.*await-sample.*no window was confirmed'):
                desktop.foreground(None, self.state, 37, activate=True, timeout=85)
        probe.assert_called_once()
        self.assertEqual(probe.call_args.kwargs['timeout'], 85)

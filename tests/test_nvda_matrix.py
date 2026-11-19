"""The host can verify an archived matrix without using QEMU, Windows or report extracts."""
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from winhoo_macos.nvda_matrix import BLOCKS, CELLS, verify


class SavedMatrixTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.owner = 'f9334d99-59cf-4205-bebd-3f16aa26a82c'
        self.source = dict(candidate='a'*40, tree='b'*40, hoovda='c'*40)
        self.inputs = dict(owner=self.owner, mode='matrix', attempt='d'*32, bundle='e'*64)
        self.host = dict(desktop={'uuid': self.owner}, source=self.source, passedBlocks=len(CELLS),
                         operation='run', mode='matrix', state='passed', shutdown='guest-confirmed',
                         windows=dict(owner=self.owner, arch='ARM64', account='Reader', console='WIN\\Reader',
                                      consoleSession=dict(connection='active', lock='unlocked', id=1, error=None,
                                                          account='WIN\\Reader')))
        self.inventory = dict(schemaVersion=1, requireReportBinding=True, blocks={b: ['Read a heading'] for b in BLOCKS})
        self.shutdown = dict(uuid=self.owner, state='off', shutdown='guest-confirmed', method='windows-shutdown',
                             attempt=self.inputs['attempt'], requestedAtUnix=100.25, timeUnix=101)
        self.guest = dict(self.inputs, exitCode=0, error=None)
        self.first = 'reports/en-US-desktop-core.json'
        self.rows, self.reports = [], {}
        for loc, layout, block in CELLS:
            cell = dict(locale=loc, layout=layout, block=block)
            self.rows.extend([{**cell, 'state': 'running'}, {**cell, 'state': 'passed', 'selected': 1, 'skipped': 1}])
            def spec(title, state):
                return dict(title=title, tests=[dict(expectedStatus=state, status='expected' if state == 'passed' else 'skipped',
                                                     results=[dict(status=state, errors=[], retry=0)])])
            self.reports[f'reports/{loc}-{layout}-{block}.json'] = dict(
                errors=[], stats=dict(expected=1, skipped=1, unexpected=0, flaky=0),
                config=dict(workers=1, projects=[{'retries': 0}],
                            metadata={'hstMatrix': {**cell, 'adapter': 'nvda', 'attempt': self.inputs['attempt']}}),
                suites=[dict(suites=[dict(specs=[spec('Read a heading', 'passed'), spec('Read a table', 'skipped')])])])

    def run_check(self, **overrides):
        inv = self.folder / 'inventory.json'
        inv.write_text(json.dumps(self.inventory))
        if self.shutdown is not None:
            (self.folder / 'shutdown.json').write_text(json.dumps(self.shutdown))
        (self.folder / 'receipt.json').write_text(json.dumps(self.host))
        (self.folder / 'guest-input.json').write_text(json.dumps(self.inputs))
        with ZipFile(self.folder / 'reports.zip', 'w') as arc:
            arc.writestr('guest.json', json.dumps(self.guest).encode('utf-8-sig'))
            arc.writestr('blocks.jsonl', '\n'.join(map(json.dumps, self.rows)))
            for name, report in self.reports.items():
                arc.writestr(name.replace('/', '\\'), json.dumps(report))
        args = dict(candidate=self.source['candidate'], tree=self.source['tree'], engine=self.source['hoovda'], owner=self.owner)
        return verify(self.folder, inv, **{**args, **overrides})

    def test_archived_cases_and_fingerprints_of_all_reports(self):
        # An extraction from a different attempt must not influence the verifier.
        (self.folder / 'guest.json').write_text('{"exitCode":1}')
        result = self.run_check()
        self.assertEqual((result['blocks'], result['retries'], result['selectedPassed']), (40, 0, 40))
        self.assertEqual(len(set(result['reportSha256'].values())), 40)

    def test_wrong_selected_case_with_identical_counters_is_rejected(self):
        specs = self.reports[self.first]['suites'][0]['suites'][0]['specs']
        specs[0]['title'], specs[1]['title'] = specs[1]['title'], specs[0]['title']
        with self.assertRaisesRegex(ValueError, 'inventory'):
            self.run_check()

    def test_second_attempt_is_rejected_even_if_statistics_claim_success(self):
        results = self.reports[self.first]['suites'][0]['suites'][0]['specs'][0]['tests'][0]['results']
        results.insert(0, {**results[0], 'status': 'timedOut'})
        with self.assertRaisesRegex(ValueError, 'retried'):
            self.run_check()

    def test_report_cannot_be_substituted_or_downgraded(self):
        metadata = self.reports[self.first]['config']['metadata']
        binding = metadata.pop('hstMatrix')
        with self.assertRaisesRegex(ValueError, 'metadata'):
            self.run_check()
        # Historical reports may omit this metadata. A supplied binding must always match.
        self.inventory.pop('requireReportBinding')
        self.assertEqual(self.run_check()['selectedPassed'], 40)
        for key, wrong in dict(locale='de-DE', layout='laptop', block='settings', adapter='synthetic', attempt='1'*32).items():
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'metadata'):
                metadata['hstMatrix'] = {**binding, key: wrong}
                self.run_check()

    def test_archived_shutdown_is_required_for_new_attempts_and_cannot_be_stale(self):
        self.shutdown['timeUnix'] = 100
        with self.assertRaisesRegex(ValueError, 'fresh guest shutdown'):
            self.run_check()
        self.shutdown['timeUnix'] = 101
        self.shutdown['attempt'] = '1'*32
        with self.assertRaisesRegex(ValueError, 'fresh guest shutdown'):
            self.run_check()
        self.shutdown = None
        (self.folder / 'shutdown.json').unlink()
        with self.assertRaises(FileNotFoundError):
            self.run_check()
        self.inventory.pop('requireReportBinding')
        self.assertEqual(self.run_check()['blocks'], 40)

    def test_wrong_commit_or_shutdown_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'candidate'):
            self.run_check(engine='1'*40)
        self.host['shutdown'] = 'monitor-confirmed'
        with self.assertRaisesRegex(ValueError, 'shutdown'):
            self.run_check()

    def test_guest_receipt_must_confirm_the_original_attempt(self):
        self.guest['attempt'] = '1'*32
        with self.assertRaisesRegex(ValueError, 'original attempt'):
            self.run_check()

    def test_repeated_block_even_with_a_passing_receipt_is_rejected(self):
        self.rows[2:4] = self.rows[:2]
        with self.assertRaisesRegex(ValueError, 'out-of-order'):
            self.run_check()

    def test_missing_archive_member_fails(self):
        del self.reports[self.first]
        with self.assertRaisesRegex(ValueError, 'missing'):
            self.run_check()

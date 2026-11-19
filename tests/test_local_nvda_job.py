"""Separate log progress and recovered guest results from a qualified host receipt."""
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from examples import local_nvda_job as jobs


class NativeJobTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'darwin' and os.getuid() != 0, 'requires the macOS user launchd domain')
    def test_launchd_submits_once_without_starting_windows(self):
        if subprocess.run(['/bin/launchctl','print',f'gui/{os.getuid()}'], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode:
            self.skipTest('no GUI launchd domain')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source, vm, job = root/'fixture', root/'vm', root/'job'
            qualifier = source/'oracle/windows-nvda/winhoo-local.py'
            qualifier.parent.mkdir(parents=True)
            qualifier.write_text("import json, pathlib, sys\n"
                                 "p = pathlib.Path(sys.argv[sys.argv.index('--evidence') + 1]).parent\n"
                                 "(p / 'started.json').write_text(json.dumps(sys.argv[1:]))\n")
            vm.mkdir()
            owner = str(uuid.uuid4())
            cli = root/'fixture-controller'
            cli.write_text('#!/bin/sh\n' + "printf '%s\\n' '" + json.dumps(
                {'uuid':owner,'state':'off','controllerLocks':{'lifecycle':'free','guestOperation':'free'}}) + "'\n")
            cli.chmod(0o700)
            def git(*args):
                return subprocess.check_output(['git',*args],cwd=source,text=True,stderr=subprocess.DEVNULL).strip()
            git('init','-q')
            git('add','oracle')
            git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
                '-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false','commit','-qm','Fixture')
            sha = git('rev-parse','HEAD')
            result = jobs.submit(job, source, vm, str(cli), sha)
            domain = f'gui/{os.getuid()}'
            try:
                deadline = time.monotonic() + 15
                while not (job/'started.json').exists() and time.monotonic() < deadline:
                    time.sleep(0.1)
                argv = jobs.read_json(job/'started.json')
                self.assertEqual(argv, ['run','--desktop',str(vm),'--candidate',sha,'--mode','matrix',
                                        '--evidence',str(job/'evidence')])
                conf = plistlib.loads((job/'job.plist').read_bytes())
                self.assertEqual(conf['EnvironmentVariables']['HST_WINHOO_BIN'], str(cli))
                self.assertFalse(conf['KeepAlive'])
                with self.assertRaisesRegex(ValueError,'still owns'):
                    jobs.submit(root/'another-job',source,vm,str(cli),sha)
                self.assertFalse((root/'another-job').exists())
            finally:
                subprocess.run(['/bin/launchctl','bootout',domain+'/'+result['label']],check=True)

    def test_original_guest_failure_survives_a_lost_host_and_incomplete_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            owner, candidate = str(uuid.uuid4()), 'a' * 40
            (job / 'receipt.json').write_text(json.dumps({
                'desktop': {'uuid': owner}, 'source': {'candidate': candidate}, 'state': 'starting',
                'mode': 'matrix', 'operation': 'run', 'passedBlocks': 0, 'shutdown': 'pending'}))
            (job / 'run.log').write_text(
                'Starting NVDA parity en-US/desktop/core\nPassed NVDA parity en-US/desktop/core in 5.1s\n'
                'Passed NVDA parity en-US/desktop/core in 5.1s\nStarting NVDA parity de-DE/desktop/actions-08\n'
                'Passed NVDA parity de-DE/desktop/actions-08 in 0.1s')
            original = {'owner': owner, 'mode': 'matrix', 'attempt': uuid.uuid4().hex, 'bundle': 'b' * 64}
            (job / 'guest-input.json').write_text(json.dumps(original))
            for code, state in ((1, 'guest-reported-failure'), (0, 'awaiting-host-verification')):
                (job / 'guest.json').write_text(json.dumps({**original, 'exitCode': code}), encoding='utf-8-sig')
                result = jobs.status(job)
                self.assertEqual((result['state'], result['reportedBlocks'], result['unfinishedLogBlock']),
                                 (state, 1, 'de-DE/desktop/actions-08'))
                self.assertEqual(result['hostReceiptState'], 'starting')
            (job / 'guest.json').write_text(json.dumps({**original, 'attempt': uuid.uuid4().hex, 'exitCode': 0}))
            with self.assertRaisesRegex(ValueError, 'original attempt'):
                jobs.status(job)

    def test_terminal_success_needs_matching_commit_owner_and_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = {'owner': str(uuid.uuid4()), 'candidate': 'c' * 40, 'label': 'demo'}
            (root / 'job.json').write_text(json.dumps(plan))
            evidence = root / 'evidence'
            evidence.mkdir()
            self.assertEqual(jobs.status(root)['state'], 'awaiting-receipt')
            receipt = {'desktop': {'uuid': plan['owner']}, 'source': {'candidate': plan['candidate']},
                       'state': 'passed', 'mode': 'matrix', 'operation': 'run',
                       'passedBlocks': 40, 'shutdown': 'guest-confirmed'}
            file = evidence / 'receipt.json'
            file.write_text(json.dumps(receipt))
            self.assertEqual(jobs.status(root)['state'], 'passed')
            for delta in ({'passedBlocks': 39}, {'shutdown': 'pending'}, {'desktop': {'uuid': str(uuid.uuid4())}},
                          {'source': {'candidate': 'e' * 40}}):
                file.write_text(json.dumps({**receipt, **delta}))
                with self.assertRaises(ValueError):
                    jobs.status(root)

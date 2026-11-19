"""Trust and restart boundaries of the local Windows desktop controller."""
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
import uuid

import winhoo
import winhoo_macos as desktop
from winhoo_macos import media


class DesktopTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.owner = str(uuid.uuid4())
        self.state = {'uuid': self.owner, 'account': 'TestUser'}

    def test_identity_matches_relocated_controller_and_rejects_guest_script_drift(self):
        fixture = self.root / 'original'
        moved = self.root / 'relocated'
        for install in (fixture, moved):
            package = install / 'winhoo_macos'
            package.mkdir(parents=True)
            (install / 'winhoo.py').write_bytes(b'host entry')
            (package / '__init__.py').write_bytes(b'controller')
            (package / 'foreground.ps1').write_bytes(b'guest probe')
        (moved / 'winhoo_macos' / '__pycache__').mkdir()
        (moved / 'winhoo_macos' / 'notes.txt').write_text('not installed by the wheel')

        def identity(install, expected=None):
            return desktop.controller_identity(install / 'winhoo.py', install / 'winhoo_macos', expected)

        start = identity(fixture)
        self.assertEqual(identity(moved, start['sha256']), start)
        (moved / 'winhoo_macos' / 'foreground.ps1').write_bytes(b'changed guest probe')
        drifted = identity(moved)
        self.assertNotEqual(start['sha256'], drifted['sha256'])
        self.assertEqual([name for name in start['files'] if start['files'][name] != drifted['files'][name]],
                         ['winhoo_macos/foreground.ps1'])
        with self.assertRaisesRegex(ValueError, 'does not match'):
            identity(moved, start['sha256'])

    def test_duplicate_forwards_and_nonloopback_specs_rejected(self):
        for port in ('2226:22', '0.0.0.0:3456:80', '80:80', '4000:65536'):
            with self.subTest(port=port), self.assertRaises(ValueError):
                desktop.forward_ports([port], 2226)
        self.assertEqual(desktop.forward_ports(['3487:80'],2226), [(2226,22),(3487,80)])

    def test_start_and_wait_attests_under_one_exclusive_lifecycle_lock(self):
        active = {'state': 'running', 'uuid': self.owner}
        for flags, expected in (([], None), (['--wait'], 'console'), (['--wait', 'transport'], 'transport')):
            with self.subTest(flags=flags):
                args = winhoo.parser().parse_args(['desktop', 'up', str(self.root), *flags])

                def attest(directory, state, timeout, *, interactive):
                    with self.assertRaisesRegex(ValueError, 'operation owns'), desktop.operation_lock(directory, 'down'):
                        self.fail('A competing shutdown acquired the lock during startup')
                    return {'owner': self.owner, 'account': 'TestUser', 'arch': 'ARM64'}

                with patch.object(desktop, 'read_state', return_value=self.state), \
                     patch.object(desktop, 'up', return_value=active), patch.object(desktop, 'wait', side_effect=attest) as wait:
                    result = desktop.dispatch(args)
                if expected:
                    wait.assert_called_once_with(self.root, self.state, 900, interactive=expected == 'console')
                    self.assertEqual(result['readiness']['owner'], self.owner)
                    self.assertEqual(result['waitedFor'], expected)
                else:
                    wait.assert_not_called()
                    self.assertEqual(result, active)

    def test_start_does_not_report_ready_when_the_requested_console_is_locked(self):
        args=winhoo.parser().parse_args(['desktop','up',str(self.root),'--wait','console','--timeout','60'])
        with patch.object(desktop,'read_state',return_value=self.state), \
             patch.object(desktop,'up',return_value={'state':'running','uuid':self.owner}), \
             patch.object(desktop,'wait',side_effect=ValueError('console-locked')), \
             patch.object(desktop,'down') as stop, self.assertRaisesRegex(ValueError,'console-locked'):
            desktop.dispatch(args)
        stop.assert_not_called()

    def test_invalid_readiness_timeout_does_not_start_the_vm(self):
        for flags in (['--timeout', '60'], ['--wait', '--timeout', '0']):
            with self.subTest(flags=flags):
                args = winhoo.parser().parse_args(['desktop', 'up', str(self.root), *flags])
                with patch.object(desktop, 'read_state', return_value=self.state), patch.object(desktop, 'up') as start:
                    with self.assertRaisesRegex(ValueError, '--timeout requires --wait'):
                        desktop.dispatch(args)
                    start.assert_not_called()

    def test_different_retained_guest_cannot_authorize_a_command(self):
        receipt = self.root / 'installed.json'
        self.assertFalse(desktop.owned_receipt(self.root, receipt.name,'owner',self.state))
        receipt.write_text(json.dumps({'owner':str(uuid.uuid4())}))
        with self.assertRaisesRegex(ValueError, 'owner'):
            desktop.owned_receipt(self.root,receipt.name,'owner',self.state)

    def test_wait_retries_only_a_temporarily_unavailable_phase(self):
        live = self.live_console(lock='unlocked')
        with patch.object(desktop,'current',return_value='running'), \
             patch.object(desktop,'phase',side_effect=[desktop.ReceiptUnavailable(), 'image-applied']), \
             patch.object(desktop.time,'sleep'), \
             patch.object(desktop,'guest',return_value=subprocess.CompletedProcess([],0,json.dumps(live),'')) as guest:
            self.assertEqual(desktop.wait(self.root,self.state,30), live)
            guest.assert_called_once()
        self.assertEqual(json.loads((self.root/'installed.json').read_text()),live)

    def live_console(self, *, lock, connection='active', account=r'WIN\TestUser', session=1):
        return {'owner':self.owner, 'account':'TESTUSER', 'computer':'WIN', 'arch':'ARM64',
                'console':account, 'consoleSession':{'id':session, 'lock':lock, 'connection':connection}}

    def test_wait_requires_the_owned_unlocked_physical_console(self):
        ready = self.live_console(lock='unlocked')
        unavailable = [
            self.live_console(lock='locked'), self.live_console(lock='unknown'),
            self.live_console(lock='unlocked', connection='disconnected'),
            self.live_console(lock='unlocked', account=r'OTHER\TestUser'),
            self.live_console(lock='unlocked', session=None),
            self.live_console(lock='unlocked', session=0),
            {k:v for k,v in ready.items() if k != 'consoleSession'},
        ]
        for pending in unavailable:
            with self.subTest(pending=pending), patch.object(desktop,'current',return_value='running'), \
                 patch.object(desktop,'phase',return_value='image-applied'), patch.object(desktop.time,'sleep'), \
                 patch.object(desktop,'guest',side_effect=[subprocess.CompletedProcess([],0,json.dumps(v),'')
                                                          for v in (pending,ready)]) as observe:
                self.assertEqual(desktop.wait(self.root,self.state,30),ready)
                self.assertEqual(observe.call_count,2)

    def test_stable_locked_console_ends_long_wait_and_creates_no_ready_receipt(self):
        clock=[0.]
        def pause(seconds): clock[0]+=seconds
        locked=self.live_console(lock='locked')
        with patch.object(desktop,'current',return_value='running'), patch.object(desktop,'phase',return_value='image-applied'), \
             patch.object(desktop.time,'monotonic',side_effect=lambda:clock[0]), patch.object(desktop.time,'sleep',side_effect=pause), \
             patch.object(desktop,'guest',return_value=subprocess.CompletedProcess([],0,json.dumps(locked),'')), \
             self.assertRaisesRegex(ValueError,'console-locked'):
            desktop.wait(self.root,self.state,900)
        self.assertGreaterEqual(clock[0],45)
        self.assertLess(clock[0],90)
        self.assertFalse((self.root/'installed.json').exists())

    def test_transport_only_wait_can_collect_after_logon_is_locked(self):
        live=self.live_console(lock='locked')
        with patch.object(desktop,'current',return_value='running'), patch.object(desktop,'phase',return_value='image-applied'), \
             patch.object(desktop,'guest',return_value=subprocess.CompletedProcess([],0,json.dumps(live),'')) as observe:
            self.assertEqual(desktop.wait(self.root,self.state,5,interactive=False),live)
        observe.assert_called_once()
        self.assertFalse((self.root/'installed.json').exists())

    def test_phase_owner_mismatch_never_reaches_the_guest(self):
        with patch.object(desktop,'current',return_value='running'), \
             patch.object(desktop.subprocess,'run',return_value=subprocess.CompletedProcess([],0,b'another-owner\n')), \
             patch.object(desktop,'guest') as command, self.assertRaisesRegex(ValueError,'unexpected owner'):
            desktop.wait(self.root,self.state,30)
        command.assert_not_called()

    def test_lost_monitor_does_not_start_another_guest(self):
        (self.root/'qemu.pid').write_text('9973')
        with patch.object(desktop,'Monitor',side_effect=ConnectionRefusedError), \
             patch.object(desktop.os,'kill') as exists, patch.object(desktop,'doctor') as boot, \
             self.assertRaises(desktop.MonitorUnavailable):
            desktop.up(self.root,self.state)
        exists.assert_called_once_with(9973,0)
        boot.assert_not_called()

    @contextmanager
    def shutdown_monitor(self, answers, states):
        monitor = Mock()
        monitor.send.side_effect = answers
        (self.root/'qemu.pid').write_text('9973')
        with patch.object(desktop,'current',side_effect=states), \
             patch.object(desktop,'Monitor') as connect, patch.object(desktop.time,'sleep'):
            connect.return_value.__enter__.return_value = monitor
            yield monitor

    def test_qmp_eof_on_quit_requires_guest_shutdown_and_original_pid_exit(self):
        with self.shutdown_monitor([{'status':'running'}, {}, {'status':'shutdown'}, ConnectionResetError()],
                                   ['running','off','off']) as monitor, \
             patch.object(desktop,'alive',side_effect=[True, False]) as alive:
            self.assertEqual(desktop.down(self.root,self.state,10)['shutdown'],'guest-confirmed')
        self.assertEqual([c.args[0] for c in monitor.send.call_args_list],
                         ['query-status','system_powerdown','query-status','quit'])
        self.assertEqual([c.args[0] for c in alive.call_args_list],[9973,9973])

    def test_disconnect_before_shutdown_or_live_pid_cannot_issue_receipt(self):
        with self.shutdown_monitor([{'status':'running'}, {}, ConnectionResetError()],['running']), \
             self.assertRaises(ConnectionError):
            desktop.down(self.root,self.state,0)
        with self.shutdown_monitor([{'status':'shutdown'}, {'status':'shutdown'}, {}],['running','off']), \
             patch.object(desktop,'alive',return_value=True), self.assertRaisesRegex(ValueError,'exit was not confirmed'):
            desktop.down(self.root,self.state,0)
        self.assertFalse((self.root/'shutdown.json').exists())

    def test_cached_media_with_wrong_hash_never_gets_replaced(self):
        (self.root/'test.esd').write_bytes(b'bad')
        spec = ('test.esd',3,'0'*64,'https://example.invalid/missing')
        with patch.multiple(media, WINDOWS=spec, OPENSSH=spec, VIRTIO=spec), \
             patch.object(media,'run') as download, self.assertRaisesRegex(ValueError,'differs'):
            media.sources(self.root)
        download.assert_not_called()
        self.assertEqual((self.root/'test.esd').read_bytes(), b'bad')

    def test_download_publishes_bytes_once_and_preserves_prior_evidence(self):
        dest = self.root/'local evidence.zip'
        self.state['sshPort'] = 2226
        def transfer(argv, **kwargs):
            self.assertIn('StrictHostKeyChecking=yes', argv)
            self.assertEqual(argv[-2], 'TestUser@127.0.0.1:evidence.zip')
            Path(argv[-1]).write_bytes(b'records')
        with patch.object(desktop.subprocess,'run',side_effect=transfer) as scp:
            desktop.get_file(self.root,self.state,'evidence.zip',dest,10)
            with self.assertRaisesRegex(ValueError,'replace'):
                desktop.get_file(self.root,self.state,'evidence.zip',dest,10)
            scp.assert_called_once()
        self.assertEqual(dest.read_bytes(),b'records')
        self.assertEqual(dest.stat().st_mode & 0o777,0o600)

    def test_archive_requires_the_bound_guest_and_exact_archive_identity(self):
        source = r"C:\ProgramData\Runner\result with user's quotes"
        ok = {'owner':self.owner, 'name':'attempt.zip', 'bytes':317, 'sha256':'3'*64}
        for change in ({}, {'owner':'other'}, {'name':'other.zip'}, {'bytes':0}, {'sha256':'missing'}):
            with self.subTest(change=change), patch.object(desktop,'guest',
                 return_value=subprocess.CompletedProcess([],0,json.dumps({**ok,**change}),'')) as guest:
                if change:
                    with self.assertRaisesRegex(ValueError,'receipt'):
                        desktop.archive_results(self.root,self.state,source,'attempt.zip',10)
                else:
                    self.assertEqual(desktop.archive_results(self.root,self.state,source,'attempt.zip',10),ok)
                self.assertNotIn(source,guest.call_args.args[2])  # data, never PowerShell code
        with patch.object(desktop,'guest') as guest,self.assertRaisesRegex(ValueError,'basename'):
            desktop.archive_results(self.root,self.state,source,'../x.zip',10)
        guest.assert_not_called()

    def test_incomplete_download_is_never_visible_as_a_result(self):
        dest = self.root/'summary.zip'
        self.state['sshPort'] = 2226
        def disconnect(argv, **kwargs):
            Path(argv[-1]).write_bytes(b'partial')
            raise subprocess.TimeoutExpired('scp',1)
        with patch.object(desktop.subprocess,'run',side_effect=disconnect), self.assertRaises(subprocess.TimeoutExpired):
            desktop.get_file(self.root,self.state,'summary.zip',dest,1)
        self.assertFalse(dest.exists())

    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin kernel lock inspection')
    def test_status_observes_controller_locks_without_acquiring_or_creating_them(self):
        args = winhoo.parser().parse_args(['desktop', 'status', str(self.root)])
        with patch.object(desktop, 'read_state', return_value={**self.state, 'ports': []}), \
             patch.object(desktop, 'current', return_value='running'), \
             patch.object(desktop, 'phase', return_value='image-applied'):
            def observed():
                return desktop.dispatch(args)['controllerLocks']
            free = {'lifecycle': 'free', 'guestOperation': 'free'}
            self.assertEqual(observed(), free)
            self.assertFalse((self.root/'lifecycle.lock').exists())
            with desktop.operation_lock(self.root, 'exec'):
                self.assertEqual(observed(), {'lifecycle': 'shared', 'guestOperation': 'exclusive'})
            with desktop.operation_lock(self.root, 'inspect'):
                self.assertEqual(observed(), {'lifecycle': 'shared', 'guestOperation': 'free'})
            with desktop.operation_lock(self.root, 'down'):
                self.assertEqual(observed(), {'lifecycle': 'exclusive', 'guestOperation': 'free'})
            self.assertEqual(observed(), free)

    @unittest.skipUnless(sys.platform == 'darwin', 'requires Darwin lock observation')
    def test_locks_only_observes_shutdown_even_when_the_monitor_cannot_reply(self):
        desktop.save(self.root / 'desktop.json', {
            **self.state, 'schema': 'winhoo.macos-desktop/1', 'directory': str(self.root),
        })
        args = winhoo.parser().parse_args(['desktop', 'status', str(self.root), '--locks-only'])
        with desktop.operation_lock(self.root, 'down'), \
             patch.object(desktop, 'Monitor', side_effect=TimeoutError('occupied')) as monitor, \
             patch.object(desktop, 'phase', side_effect=AssertionError('read guest media')), \
             patch.object(desktop, 'owned_receipt', side_effect=AssertionError('read guest receipt')):
            self.assertEqual(desktop.dispatch(args), {
                'uuid': self.owner,
                'controllerLocks': {'lifecycle': 'exclusive', 'guestOperation': 'free'},
            })
            monitor.assert_not_called()

    def test_foreground_command_allows_observation_but_blocks_writes_and_poweroff(self):
        with desktop.operation_lock(self.root,'exec'):
            for op in ('inspect','get','foreground'):
                with self.subTest(op=op),desktop.operation_lock(self.root,op): pass
            for op in ('exec','put','archive','up','down','wait'):
                with self.subTest(op=op),self.assertRaisesRegex(ValueError,'lifecycle'):
                    with desktop.operation_lock(self.root,op): pass
        with desktop.operation_lock(self.root,'down'),self.assertRaisesRegex(ValueError,'lifecycle'):
            with desktop.operation_lock(self.root,'inspect'): pass

    def test_switch_to_guest_local_testing_preserves_ssh_and_requires_stopped_vm(self):
        cfg = {**self.state,'sshPort':2226,'ports':[[2226,22],[3002,3000]]}
        desktop.save(self.root/'desktop.json',cfg)
        with patch.object(desktop,'current',return_value='running'),self.assertRaisesRegex(ValueError,'Shut down'):
            desktop.set_forwards(self.root,cfg,[])
        self.assertEqual(json.loads((self.root/'desktop.json').read_text()),cfg)
        with patch.object(desktop,'current',return_value='off'):
            desktop.set_forwards(self.root,cfg,[])
        saved=json.loads((self.root/'desktop.json').read_text())
        self.assertEqual(saved,{**cfg,'ports':[[2226,22]]})

    def test_loopback_check_considers_every_listener_on_the_requested_port(self):
        listeners=[{'LocalPort':22,'LocalAddress':'0.0.0.0'},{'LocalPort':8700,'LocalAddress':'::1'},
                   {'LocalPort':8700,'LocalAddress':'127.0.0.1'}]
        desktop.check_loopback(listeners,[8700])
        for address in ('::','0.0.0.0','10.0.2.15'):
            with self.subTest(address=address),self.assertRaisesRegex(ValueError,'outside loopback'):
                desktop.check_loopback(listeners+[{'LocalPort':8700,'LocalAddress':address}],[8700])
        with self.assertRaisesRegex(ValueError,'no listener'):
            desktop.check_loopback(listeners,[8701])

    def test_collect_requires_a_stopped_owned_guest_and_proof_of_shutdown(self):
        target=self.root/'result'
        with patch.object(desktop,'current',return_value='running'),patch.object(desktop,'up') as boot, \
             self.assertRaisesRegex(ValueError,'stopped'):
            desktop.collect(self.root,self.state,'result.zip',target,20)
        boot.assert_not_called()
        desktop.save(self.root/'installed.json',{'owner':self.owner})
        desktop.save(self.root/'shutdown.json',{'uuid':self.owner,'shutdown':'guest-confirmed'})
        def transfer(d,s,name,path,timeout): path.write_bytes(b'original results')
        with patch.object(desktop,'current',return_value='off'),patch.object(desktop,'up',return_value={'state':'running','uuid':self.owner}), \
             patch.object(desktop,'wait') as attest,patch.object(desktop,'get_file',side_effect=transfer),patch.object(desktop,'down',return_value={}), \
             self.assertRaisesRegex(ValueError,'not confirmed'):
            desktop.collect(self.root,self.state,'result.zip',target,20)
        attest.assert_called_once_with(self.root,self.state,20,interactive=False)
        self.assertFalse(target.exists())

    def test_collect_result_directory_attests_before_archiving_and_publishes_after_shutdown(self):
        target = self.root/'recovered.zip'
        desktop.save(self.root/'installed.json', {'owner':self.owner})
        desktop.save(self.root/'shutdown.json', {'uuid':self.owner,'shutdown':'guest-confirmed'})
        events = []
        def record(label, result):
            def call(*args, **kwargs):
                events.append(label)
                self.assertFalse(target.exists())
                if label == 'wait': self.assertFalse(kwargs['interactive'])
                return result
            return call
        original = self.root/'guest-copy'
        original.write_bytes(b'original trace')
        identity = {'owner':self.owner,'name':'traces.zip','bytes':original.stat().st_size,'sha256':media.digest(original)}
        def download(args, **kwargs):
            events.append('download')
            Path(args[-1]).write_bytes(original.read_bytes())
        with patch.object(desktop,'current',return_value='off'), \
             patch.object(desktop,'up',side_effect=record('up',{'state':'running','uuid':self.owner})), \
             patch.object(desktop,'wait',side_effect=record('wait',{})), \
             patch.object(desktop,'archive_results',side_effect=record('archive',identity)), \
             patch.object(desktop,'ssh_args',return_value=[]),patch.object(desktop.subprocess,'run',side_effect=download), \
             patch.object(desktop,'down',side_effect=record('down',{'state':'off','uuid':self.owner,'shutdown':'guest-confirmed'})):
            result=desktop.collect(self.root,self.state,'traces.zip',target,30,source=r'C:\ProgramData\Example\results')
        self.assertEqual(events,['up','wait','archive','download','down'])
        self.assertEqual(result['archive'],identity)
        self.assertEqual(target.read_bytes(),original.read_bytes())

    def test_collect_from_interrupted_running_owner_skips_boot_and_requires_verified_download(self):
        target = self.root/'recovered.zip'
        source = r'C:\ProgramData\MyRunner\completed-results'
        desktop.save(self.root/'installed.json', {'owner': self.owner})
        archive = {'owner': self.owner, 'name': 'original.zip', 'bytes': 5, 'sha256': '0'*64}
        stop = {'uuid': self.owner, 'state': 'off', 'shutdown': 'guest-confirmed'}
        def transfer(d, state, name, path, timeout, *, archive):
            path.write_bytes(b'trace')
        with patch.object(desktop, 'current', return_value='running'), patch.object(desktop, 'up') as boot, \
             patch.object(desktop, 'wait') as attest, patch.object(desktop, 'archive_results', return_value=archive), \
             patch.object(desktop, 'get_file', side_effect=ValueError('digest mismatch')) as get, \
             patch.object(desktop, 'down', return_value=stop) as down:
            with self.assertRaisesRegex(ValueError, 'owner'):
                desktop.collect(self.root, self.state, 'original.zip', target, 30, source=source,
                                running_owner='00000000-0000-0000-0000-000000000001')
            attest.assert_not_called()
            with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                desktop.collect(self.root, self.state, 'original.zip', target, 30, source=source, running_owner=self.owner)
            down.assert_not_called()
            self.assertFalse(target.exists())
            get.side_effect = transfer
            receipt = desktop.collect(self.root, self.state, 'original.zip', target, 30, source=source, running_owner=self.owner)
            self.assertEqual(get.call_args.kwargs['archive'], archive)
            attest.assert_called_with(self.root, self.state, 30, interactive=False)
            boot.assert_not_called()
            down.assert_called_once()
            self.assertEqual((target.read_bytes(), receipt['shutdown']), (b'trace', 'guest-confirmed'))

    def test_bad_download_never_publishes_an_archive(self):
        target=self.root/'downloaded.zip'
        for size in (4, 17):
            with self.subTest(size=size),patch.object(desktop,'ssh_args',return_value=[]), \
                 patch.object(desktop.subprocess,'run',side_effect=lambda args,**kwargs:Path(args[-1]).write_bytes(b'bad!')), \
                 self.assertRaisesRegex(ValueError,'does not match'):
                desktop.get_file(self.root,self.state,'result.zip',target,5,archive={'bytes':size,'sha256':'0'*64})
            self.assertFalse(target.exists())

    def test_new_boot_archives_shutdown_proof_without_authorizing_a_later_crash(self):
        receipt={'uuid':self.owner,'shutdown':'guest-confirmed','timeUnix':4}
        desktop.save(self.root/'shutdown.json',receipt)
        desktop.archive_shutdown(self.root,self.state)
        self.assertFalse((self.root/'shutdown.json').exists())
        [archived]=(self.root/'shutdown-history').iterdir()
        self.assertEqual(json.loads(archived.read_text()),receipt)
        desktop.save(self.root/'shutdown.json',{**receipt,'uuid':'different-vm'})
        with self.assertRaisesRegex(ValueError,'owner'):
            desktop.archive_shutdown(self.root,self.state)
        self.assertTrue((self.root/'shutdown.json').is_file())

    @unittest.skipUnless(desktop.platform.system() == 'Darwin', 'macOS power assertions')
    def test_idle_sleep_assertion_releases_with_the_owned_process(self):
        import time
        with subprocess.Popen(['/bin/sleep','15']) as owner:
            guard = desktop.guard_idle_sleep(owner.pid)
            try:
                end=time.monotonic()+5
                while time.monotonic()<end:
                    info=subprocess.check_output(['/usr/bin/pmset','-g','assertions'],text=True)
                    if any(f'pid {guard.pid}(caffeinate)' in l and 'PreventUserIdleSystemSleep' in l for l in info.splitlines()):
                        break
                    time.sleep(.1)
                else:
                    self.fail('macOS did not report the owned idle-sleep assertion')
                owner.terminate()
                owner.wait(timeout=3)
                self.assertEqual(guard.wait(timeout=5),0)
            finally:
                if owner.poll() is None: owner.terminate()
                if guard.poll() is None: guard.terminate()
                guard.wait(timeout=3)

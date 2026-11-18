"""Trust and restart boundaries of the local Windows desktop controller."""
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock
import uuid

import winhoo_macos as desktop
from winhoo_macos import media


class DesktopTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.owner = str(uuid.uuid4())
        self.state = {'uuid': self.owner, 'account': 'TestUser'}

    def test_duplicate_forwards_and_nonloopback_specs_rejected(self):
        for port in ('2226:22', '0.0.0.0:3456:80', '80:80', '4000:65536'):
            with self.subTest(port=port), self.assertRaises(ValueError):
                desktop.forward_ports([port], 2226)
        self.assertEqual(desktop.forward_ports(['3487:80'],2226), [(2226,22),(3487,80)])

    def test_different_retained_guest_cannot_authorize_a_command(self):
        receipt = self.root / 'installed.json'
        self.assertFalse(desktop.owned_receipt(self.root, receipt.name,'owner',self.state))
        receipt.write_text(json.dumps({'owner':str(uuid.uuid4())}))
        with self.assertRaisesRegex(ValueError, 'owner'):
            desktop.owned_receipt(self.root,receipt.name,'owner',self.state)

    def test_wait_retries_only_a_temporarily_unavailable_phase(self):
        live = {'owner':self.owner,'account':'TESTUSER','arch':'ARM64','console':r'WIN\TestUser'}
        with patch.object(desktop,'current',return_value='running'), \
             patch.object(desktop,'phase',side_effect=[desktop.ReceiptUnavailable(), 'image-applied']), \
             patch.object(desktop.time,'sleep'), \
             patch.object(desktop,'guest',return_value=subprocess.CompletedProcess([],0,json.dumps(live),'')) as guest:
            self.assertEqual(desktop.wait(self.root,self.state,30), live)
            guest.assert_called_once()
        self.assertEqual(json.loads((self.root/'installed.json').read_text()),live)

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

    def test_incomplete_download_is_never_visible_as_a_result(self):
        dest = self.root/'summary.zip'
        self.state['sshPort'] = 2226
        def disconnect(argv, **kwargs):
            Path(argv[-1]).write_bytes(b'partial')
            raise subprocess.TimeoutExpired('scp',1)
        with patch.object(desktop.subprocess,'run',side_effect=disconnect), self.assertRaises(subprocess.TimeoutExpired):
            desktop.get_file(self.root,self.state,'summary.zip',dest,1)
        self.assertFalse(dest.exists())

    def test_foreground_command_allows_observation_but_blocks_writes_and_poweroff(self):
        with desktop.operation_lock(self.root,'exec'):
            for op in ('inspect','get'):
                with self.subTest(op=op),desktop.operation_lock(self.root,op): pass
            for op in ('exec','put','up','down','wait'):
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
             patch.object(desktop,'wait'),patch.object(desktop,'get_file',side_effect=transfer),patch.object(desktop,'down',return_value={}), \
             self.assertRaisesRegex(ValueError,'not confirmed'):
            desktop.collect(self.root,self.state,'result.zip',target,20)
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

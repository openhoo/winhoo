#!/usr/bin/env python3
"""Exercise a real Windows engine, guest SSH, file exchange and cleanup."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from winhoo import Controller, WinhooError  # noqa: E402


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def main() -> int:
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument('--context')
    arguments.add_argument('--host-ssh-config', type=Path)
    arguments.add_argument('--state-dir', type=Path, default=ROOT / '.local')
    arguments.add_argument('--image', default='winhoo:0.1.0')
    arguments.add_argument('--isolation', choices=('hyperv', 'process'), default='hyperv')
    arguments.add_argument('--report', type=Path, default=ROOT / 'artifacts/runtime-smoke.json')
    args = arguments.parse_args()
    controller = Controller(args.context, args.state_dir, host_ssh_config=args.host_ssh_config)
    name = 'smoke-' + uuid.uuid4().hex[:12]
    report = {'startedAt': datetime.now(timezone.utc).isoformat(), 'name': name, 'steps': [], 'passed': False}

    def passed(step: str) -> None:
        report['steps'].append({'step': step, 'passed': True})
        print('PASS ' + step, flush=True)

    def execute(source: str, expected_exit: int = 0) -> str:
        result = controller.execute(name, source, capture=True, timeout=45)
        if result.returncode != expected_exit:
            raise WinhooError('Expected exit %s, received %s. %s %s' % (
                expected_exit, result.returncode, result.stdout, result.stderr))
        return result.stdout.strip()

    try:
        report['host'] = controller.doctor()
        passed('Windows engine and supported transport')
        report['instance'] = controller.up(name, image=args.image, isolation=args.isolation)
        passed('native container boot and authenticated winhoo SSH')
        state = controller.load(name)
        record = controller.inspect(state)
        if record.get('HostConfig', {}).get('PortBindings'):
            raise WinhooError('The container unexpectedly publishes host ports.')
        passed('no published host ports')

        expected_unicode = 'winhoo: Grüße, €, 日本語'
        if execute('[Console]::Out.WriteLine(' + ps_quote(expected_unicode) + ')') != expected_unicode:
            raise WinhooError('Unicode command transport/output changed the text.')
        passed('UTF-8 PowerShell input and output')
        execute('cmd.exe /c exit 23', 23)
        execute('exit 37', 37)
        execute("throw 'intentional winhoo smoke failure'", 1)
        passed('native and PowerShell failure exit codes')

        with tempfile.TemporaryDirectory(prefix='winhoo-smoke-') as temporary:
            folder = Path(temporary)
            script_path = folder / 'unicode-script.ps1'
            script_path.write_text(
                '[Console]::Out.WriteLine(' + ps_quote(expected_unicode) + ')\n'
                "[Console]::Out.WriteLine($PSScriptRoot)\n", encoding='utf-8')
            script_result = controller.script(name, script_path, capture=True)
            script_lines = script_result.stdout.strip().splitlines()
            if (script_result.returncode != 0 or len(script_lines) != 2 or
                    script_lines[0] != expected_unicode or
                    script_lines[1].replace('\\', '/').lower() != 'c:/work'):
                raise WinhooError('Staged Unicode script or its PSScriptRoot changed: ' + str(script_lines))
            passed('UTF-8 file staging and real PSScriptRoot')

            original = folder / 'payload ü & dollar$.txt'
            original.write_bytes(b'winhoo\x00\x01\xff\r\nfile exchange\n')
            remote = 'C:/work/' + original.name
            controller.copy(name, str(original), remote)
            remote_hash = execute('(Get-FileHash -LiteralPath ' + ps_quote(remote) + ' -Algorithm SHA256).Hash')
            if remote_hash.lower() != hashlib.sha256(original.read_bytes()).hexdigest():
                raise WinhooError('Uploaded file hash differs inside the container.')
            downloaded = folder / 'downloaded.txt'
            controller.copy(name, remote, str(downloaded), download=True)
            if downloaded.read_bytes() != original.read_bytes():
                raise WinhooError('Downloaded file bytes differ.')
        passed('binary file round trip with Unicode and shell metacharacters')

        pin = (controller.instance_dir(name) / 'known_hosts').read_bytes()
        controller.down(name)
        controller.up(name, image=args.image, isolation=args.isolation)
        if (controller.instance_dir(name) / 'known_hosts').read_bytes() != pin:
            raise WinhooError('Recreating the container changed its retained host-key pin.')
        execute("[Console]::Out.WriteLine('recreated')")
        passed('recreate with retained SSH identity and fresh runtime account')
        report['passed'] = True
    except (Exception, KeyboardInterrupt) as exc:
        report['error'] = str(exc) or type(exc).__name__
        print('FAIL ' + report['error'], file=sys.stderr)
        try:
            state = controller.load(name)
            controller.inspect(state)
            logs = controller.docker(state['context'], 'logs', '--tail', '100', state['container_id'], check=False)
            report['containerLogs'] = (logs.stdout or '') + (logs.stderr or '')
        except Exception:
            pass
    finally:
        if (controller.instance_dir(name) / 'state.json').is_file():
            try:
                controller.down(name, purge=True)
                passed('owned container, key volume and local state cleanup')
            except Exception as exc:
                report['passed'] = False
                report['cleanupError'] = str(exc)
                print('FAIL cleanup: ' + str(exc), file=sys.stderr)
        report['finishedAt'] = datetime.now(timezone.utc).isoformat()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('Report: ' + str(args.report.resolve()))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

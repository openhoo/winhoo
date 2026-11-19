"""Run one HooSaidThat NVDA matrix on macOS independently of the submitting terminal."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import uuid


CELL = r'(en-US|de-DE)/(desktop|laptop)/(core|actions-0[1-8]|settings)'
PASSED = re.compile(r'^Passed NVDA parity (' + CELL + r') in [\d.]+s$')
STARTED = re.compile(r'^Starting NVDA parity (' + CELL + r')$')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def power_source(output):
    # pmset reports the active source even on desktop Macs without a battery.
    match = re.search(r"^Now drawing from '(AC Power|Battery Power|UPS Power)'\s*$", output, re.M)
    return match[1] if match else 'unknown'


def read_power_source():
    try:
        output = subprocess.check_output(['/usr/bin/pmset', '-g', 'batt'], text=True, timeout=5,
                                         env={**os.environ, 'LC_ALL': 'C'}, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return 'unknown'
    return power_source(output)


def progress(evidence):
    """Log markers are progress hints. A reader can see a partially written last line."""
    passed, current = set(), None
    try:
        with (evidence / 'run.log').open(encoding='utf-8', errors='replace') as log:
            for line in log:
                if not line.endswith('\n'):
                    continue
                line = line.rstrip('\r\n')
                if match := STARTED.fullmatch(line):
                    current = match[1]
                elif match := PASSED.fullmatch(line):
                    passed.add(match[1])
                    if current == match[1]:
                        current = None
    except FileNotFoundError:
        pass
    return {'reportedBlocks': len(passed), 'unfinishedLogBlock': current}


def status(job):
    # Also inspect an existing consumer-run matrix without attaching to Windows.
    plan = read_json(job / 'job.json') if (job / 'job.json').is_file() else None
    evidence = job / 'evidence' if plan else job
    result = {'state': 'awaiting-receipt', **progress(evidence)}
    if plan:
        result.update({key: plan[key] for key in ('candidate', 'owner', 'label')})
    if not (evidence / 'receipt.json').exists():
        if not plan:
            raise ValueError('Expected a submitted job or a matrix evidence directory')
        return result
    receipt = read_json(evidence / 'receipt.json')
    candidate = receipt['source']['candidate']
    owner = receipt['desktop']['uuid']
    if (not re.fullmatch('[0-9a-f]{40}', candidate) or str(uuid.UUID(owner)) != owner
            or receipt.get('mode') != 'matrix' or receipt.get('operation') != 'run'
            or (plan and (candidate != plan['candidate'] or owner != plan['owner']))):
        raise ValueError('The matrix receipt is not for this job and desktop')
    if receipt['state'] == 'passed' and (receipt.get('passedBlocks') != 40 or receipt.get('shutdown') != 'guest-confirmed'):
        raise ValueError('Incomplete terminal matrix receipt')
    result.update(candidate=candidate, owner=owner, state=receipt['state'],
                  shutdown=receipt['shutdown'], receiptBlocks=receipt['passedBlocks'])
    if receipt['state'] not in ('failed', 'passed') and (evidence / 'guest.json').is_file():
        actual, expected = read_json(evidence / 'guest.json'), read_json(evidence / 'guest-input.json')
        if (expected.get('owner') != owner or expected.get('mode') != 'matrix'
                or not re.fullmatch(r'[a-f0-9]{32}', expected['attempt'])
                or not re.fullmatch(r'[a-f0-9]{64}', expected['bundle'])
                or any(actual.get(k) != expected[k] for k in ('owner', 'mode', 'attempt', 'bundle'))
                or type(actual.get('exitCode')) is not int):
            raise ValueError('The recovered guest result is not for the original attempt')
        result.update(hostReceiptState=receipt['state'], guestExitCode=actual['exitCode'],
                      state='guest-reported-failure' if actual['exitCode'] else 'awaiting-host-verification')
    elif receipt['state'] == 'starting' and (result['unfinishedLogBlock'] or result['reportedBlocks']):
        # Log markers show where the attempt got to; only the host can confirm success.
        result.update(hostReceiptState='starting', state='awaiting-guest-result'
                      if result['reportedBlocks'] == 40 else 'matrix-started')
    return result


def configuration(job, source, desktop, winhoo, plan):
    return {'Label': plan['label'], 'RunAtLoad': True, 'KeepAlive': False, 'Umask': 0o077,
            'ProgramArguments': [sys.executable, str(source / 'oracle/windows-nvda/winhoo-local.py'),
                                 'run', '--desktop', str(desktop), '--candidate', plan['candidate'],
                                 '--mode', 'matrix', '--evidence', str(job / 'evidence'),
                                 *(['--adopt-running'] if plan.get('adoptRunning') else [])],
            'WorkingDirectory': str(source),
            'EnvironmentVariables': {'HST_WINHOO_BIN': str(winhoo), 'PATH': os.environ['PATH']},
            'StandardOutPath': str(job / 'host.log'), 'StandardErrorPath': str(job / 'host-errors.log')}


@contextmanager
def reserve(desktop, job):
    """Close the gap between launchd accepting a job and the guest lease being taken."""
    import fcntl
    with (desktop / 'nvda-matrix-submission.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.seek(0)
        previous = lock.read().strip()
        if previous:
            other = Path(json.loads(previous))
            receipt = status(other)
            if (receipt['state'] not in ('passed', 'failed')
                    or receipt['shutdown'] not in ('guest-confirmed', 'not-acquired')):
                raise ValueError(f'A submitted matrix still owns this desktop; check status {other}')
        def record():
            lock.seek(0)
            lock.truncate()
            lock.write(json.dumps(str(job)) + '\n')
            lock.flush()
            os.fsync(lock.fileno())
        yield record


def check_desktop(live, running_owner):
    owner = str(uuid.UUID(live['uuid']))
    if any(live['controllerLocks'][k] != 'free' for k in ('lifecycle', 'guestOperation')):
        raise ValueError('The desktop controller is busy; inspect its original operation')
    if running_owner is None:
        if live['state'] != 'off':
            raise ValueError('An idle, powered-off desktop is required; inspect the original job')
    elif live['state'] != 'running' or running_owner != owner:
        raise ValueError('The running desktop must match the supplied owner UUID')
    return owner


def submit(job, source, desktop, winhoo, candidate, *, running_owner=None):
    if sys.platform != 'darwin' or os.getuid() == 0:
        raise ValueError('Submit from the logged-in macOS account')
    if not re.fullmatch('[0-9a-f]{40}', candidate):
        raise ValueError('A full candidate commit is required')
    power = read_power_source()
    if power != 'AC Power':
        raise ValueError(f'Connect the Mac to AC power before starting the NVDA matrix (current source: {power})')
    source, desktop = source.resolve(strict=True), desktop.resolve(strict=True)
    # Preserve the venv entry point selected by the caller.
    selected = shutil.which(winhoo)
    if not selected or not (source / 'oracle/windows-nvda/winhoo-local.py').is_file():
        raise ValueError('The selected WinHoo executable or HooSaidThat qualifier is missing')
    winhoo = Path(selected).absolute()
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=source, text=True, timeout=20).strip()
    if git('rev-parse', 'HEAD') != candidate or git('status', '--porcelain'):
        raise ValueError('The selected HooSaidThat checkout must be clean and at the candidate')
    with reserve(desktop, job) as record:
        live = json.loads(subprocess.check_output([str(winhoo), 'desktop', 'status', str(desktop)], timeout=30))
        owner = check_desktop(live, running_owner)
        plan = {'candidate': candidate, 'owner': owner, 'powerAtSubmit': power,
                'adoptRunning': running_owner is not None,
                'label': 'dev.openhoo.winhoo.nvda.' + uuid.uuid4().hex}
        job.mkdir(mode=0o700)
        (job / 'job.json').write_text(json.dumps(plan, indent=2) + '\n')
        plist = job / 'job.plist'
        with plist.open('xb') as out:
            plistlib.dump(configuration(job, source, desktop, winhoo, plan), out)
        # If bootstrap times out, retain the original job for reconciliation.
        record()
        subprocess.run(['/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', str(plist)], check=True, timeout=30)
    return {'job': str(job), **plan}


def run():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    start = subs.add_parser('start')
    start.add_argument('job', type=Path, help='New private directory for the job and its evidence')
    start.add_argument('--source', type=Path, required=True)
    start.add_argument('--desktop', type=Path, required=True)
    start.add_argument('--candidate', required=True)
    start.add_argument('--running-owner', metavar='UUID', help='Adopt your already running diagnostic desktop with this exact owner; the job will shut it down')
    start.add_argument('--winhoo', default='winhoo')
    subs.add_parser('status').add_argument('job', type=Path)
    args = parser.parse_args()
    job = args.job.expanduser().absolute()
    if job != job.resolve():
        raise ValueError('Use a canonical job path without symbolic links')
    result = status(job) if args.command == 'status' else submit(job, args.source, args.desktop, args.winhoo, args.candidate, running_owner=args.running_owner)
    if args.command == 'status' and sys.platform == 'darwin' and result['state'] not in ('passed', 'failed'):
        result['currentHostPower'] = read_power_source()
        if result['currentHostPower'] != 'AC Power':
            result['powerWarning'] = 'Check AC power; macOS can suspend the guest on low battery.'
    print(json.dumps(result, indent=2))


def main():
    try:
        run()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0

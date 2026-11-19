"""Verify saved HooSaidThat native NVDA evidence; never start or connect to a VM."""
import hashlib
from itertools import product
import json
import math
import re
import uuid
from zipfile import BadZipFile, ZipFile

BLOCKS = ('core', *(f'actions-{n:02}' for n in range(1, 9)), 'settings')
CELLS = tuple(product(('en-US', 'de-DE'), ('desktop', 'laptop'), BLOCKS))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(value, expected):
    return type(value) is int and value == expected


def unique_keys(pairs):
    obj = {}
    for key, value in pairs:
        require(key not in obj, 'Duplicate JSON key in matrix evidence or inventory')
        obj[key] = value
    return obj


def parse(data):
    return json.loads(data, object_pairs_hook=unique_keys)


def specs(suite):
    for spec in suite.get('specs', []):
        yield spec
    for child in suite.get('suites', []):
        yield from specs(child)


def report_counts(report, wanted):
    require(report['errors'] == [], 'Playwright reported a runner error')
    config = report['config']
    require(integer(config['workers'], 1) and len(config['projects']) == 1 and
            integer(config['projects'][0]['retries'], 0), 'Expected one worker, one project and no retries')
    selected, skipped, titles = set(), 0, set()
    for suite in report['suites']:
        for spec in specs(suite):
            title = spec['title']
            require(type(title) is str and title and title not in titles and len(spec['tests']) == 1,
                    'Expected a unique spec title and a single project result per spec')
            titles.add(title)
            test = spec['tests'][0]
            require(len(test['results']) == 1, 'A test is missing its result or was retried')
            result = test['results'][0]
            state = result['status']
            require(integer(result['retry'], 0) and result['errors'] == [] and
                    state == test['expectedStatus'] and state in ('passed', 'skipped') and
                    test['status'] == ('expected' if state == 'passed' else 'skipped'),
                    'Unexpected result for a selected or skipped test')
            if state == 'passed':
                selected.add(title)
            else:
                skipped += 1
    require(selected == wanted, 'Selected spec titles differ from the expected inventory')
    stats = report['stats']
    for name, count in [('expected', len(selected)), ('skipped', skipped), ('unexpected', 0), ('flaky', 0)]:
        require(integer(stats[name], count), 'Playwright statistics differ from the individual tests')
    return len(selected), skipped


def verify(evidence, inventory, *, candidate, tree, engine, owner=None):
    """The inventory belongs to the consumer. Do not infer expected titles from the report."""
    try:
        return _verify(evidence, inventory, candidate, tree, engine, owner)
    except (KeyError, IndexError, TypeError, AttributeError, RecursionError, BadZipFile) as exc:
        raise ValueError('Malformed NVDA matrix evidence or inventory') from exc


def _verify(folder, inventory, candidate, tree, engine, owner):
    source = {'candidate': candidate, 'tree': tree, 'hoovda': engine}
    require(all(re.fullmatch('[0-9a-f]{40}', v) for v in source.values()), 'Expected three full Git object IDs')
    raw_inventory = inventory.read_bytes()
    expected = parse(raw_inventory)
    require(set(expected) <= {'schemaVersion', 'requireReportBinding', 'blocks'} and
            integer(expected['schemaVersion'], 1) and set(expected['blocks']) == set(BLOCKS),
            'Inventory must declare every matrix block in schemaVersion 1')
    require(type(expected.get('requireReportBinding', False)) is bool, 'requireReportBinding must be a boolean')
    for block, titles in expected['blocks'].items():
        require(type(titles) is list and titles and all(type(t) is str and t.strip() for t in titles)
                and len(set(titles)) == len(titles), f'Expected a nonempty list of distinct titles for {block}')

    host = parse((folder / 'receipt.json').read_bytes())
    require(host['source'] == source, 'Receipt belongs to another candidate, tree or engine')
    require((host['operation'], host['mode'], host['state'], host['shutdown']) ==
            ('run', 'matrix', 'passed', 'guest-confirmed') and integer(host['passedBlocks'], len(CELLS)),
            'A passing terminal matrix and guest-confirmed shutdown are required')
    actual_owner = host['desktop']['uuid']
    require(str(uuid.UUID(actual_owner)) == actual_owner and (owner is None or owner == actual_owner),
            'Unexpected desktop owner')
    windows = host['windows']
    session = windows['consoleSession']
    def username(s):
        return s.rsplit('\\', 1)[-1].casefold()
    account = username(windows['account'])
    require(windows['owner'] == actual_owner and windows['arch'] == 'ARM64' and
            session['connection'] == 'active' and session['lock'] == 'unlocked' and not session['error'] and
            type(session['id']) is int and session['id'] > 0 and account and
            username(session['account']) == username(windows['console']) == account,
            'Expected the dedicated account on the active, unlocked Windows console')

    inputs = parse((folder / 'guest-input.json').read_bytes())
    require(inputs['owner'] == actual_owner and inputs['mode'] == 'matrix' and
            re.fullmatch('[0-9a-f]{32}', inputs['attempt']) and re.fullmatch('[0-9a-f]{64}', inputs['bundle']),
            'Guest input is not bound to the expected desktop and matrix attempt')
    shutdown_path = folder / 'shutdown.json'
    if shutdown_path.exists() or expected.get('requireReportBinding', False):
        shutdown = parse(shutdown_path.read_bytes())
        required = dict(uuid=actual_owner, state='off', shutdown='guest-confirmed',
                        method='windows-shutdown', attempt=inputs['attempt'])
        requested, completed = shutdown['requestedAtUnix'], shutdown['timeUnix']
        require(all(shutdown.get(k) == v for k, v in required.items())
                and all(type(t) in (float, int) and math.isfinite(t) for t in (requested, completed))
                and 0 < requested <= completed, 'No fresh guest shutdown receipt for this attempt and owner')
    names = ['reports/' + '-'.join(cell) + '.json' for cell in CELLS]
    selected, hashes = 0, {}
    with ZipFile(folder / 'reports.zip') as archive:
        members = {}
        for entry in archive.infolist():
            name = entry.filename.replace('\\', '/')
            if entry.is_dir() and name == 'reports/':
                continue
            require(name not in members and entry.file_size <= 32 * 1024 * 1024,
                    'Duplicate or oversized member in the guest report archive')
            members[name] = entry
        require(set(members) == {*names, 'blocks.jsonl', 'guest.json'}, 'Guest report archive has missing or extra files')
        guest = parse(archive.read(members['guest.json']))
        require(all(guest[k] == v for k, v in inputs.items()) and integer(guest['exitCode'], 0) and not guest['error'],
                'Guest result does not confirm the original attempt and input bundle')
        rows = [parse(line) for line in archive.read(members['blocks.jsonl']).decode('utf-8-sig').splitlines()]
        require(len(rows) == 2 * len(CELLS), 'Incomplete or repeated block transitions')
        for n, (cell, name) in enumerate(zip(CELLS, names)):
            try:
                start, end = rows[n*2:n*2+2]
                for row, state in ((start, 'running'), (end, 'passed')):
                    require(tuple(row[k] for k in ('locale', 'layout', 'block', 'state')) == (*cell, state),
                            'Missing, repeated or out-of-order block')
                data = archive.read(members[name])
                report = parse(data)
                metadata = report['config'].get('metadata', {})
                if 'hstMatrix' in metadata or expected.get('requireReportBinding', False):
                    binding = dict(zip(('locale', 'layout', 'block'), cell), adapter='nvda', attempt=inputs['attempt'])
                    require(isinstance(metadata.get('hstMatrix'), dict) and
                            all(metadata['hstMatrix'].get(k) == v for k, v in binding.items()),
                            'Report metadata does not identify this native block and attempt')
                passed, skipped = report_counts(report, set(expected['blocks'][cell[2]]))
                require(integer(end['selected'], passed) and integer(end['skipped'], skipped),
                        'Block counters differ from the individual tests')
                selected += passed
                hashes[name] = hashlib.sha256(data).hexdigest()
            except ValueError as exc:
                raise ValueError(f'{name}: {exc}') from exc
    return {'schemaVersion': 1, 'source': source, 'owner': actual_owner, 'attempt': inputs['attempt'],
            'bundle': inputs['bundle'], 'shutdown': host['shutdown'], 'blocks': len(CELLS),
            'selectedPassed': selected, 'retries': 0, 'inventorySha256': hashlib.sha256(raw_inventory).hexdigest(),
            'reportSha256': hashes}

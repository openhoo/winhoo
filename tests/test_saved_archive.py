import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
import zipfile

from winhoo_macos.archive_info import inspect


class SavedArchiveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.archive = root / 'renamed-local-results.zip'
        self.receipt = root / 'guest.json'
        self.owner = str(uuid4())
        with zipfile.ZipFile(self.archive, 'w') as z:
            z.writestr('./one/trace.zip', b'first')
            z.writestr('./two/trace.zip', b'second')
            z.writestr('./other/report.json', b'{}')
        self.record = {'name': 'guest-original.zip', 'owner': self.owner,
                       'bytes': self.archive.stat().st_size,
                       'sha256': hashlib.sha256(self.archive.read_bytes()).hexdigest()}
        self.receipt.write_text(json.dumps(self.record))

    def test_renamed_download_uses_guest_hash_and_returns_every_exact_match(self):
        result = inspect(self.archive, self.receipt, owner=self.owner, find='trace.zip')
        self.assertEqual(result['files'], 3)
        self.assertEqual(result['matches'], [{'path': './one/trace.zip', 'bytes': 5},
                                            {'path': './two/trace.zip', 'bytes': 6}])
        with self.assertRaisesRegex(ValueError, 'No matching'):
            inspect(self.archive, self.receipt, find='trace')

    def test_collection_owner_and_independent_expected_owner(self):
        wrapper = {'uuid': self.owner, 'archive': self.record}
        self.receipt.write_text(json.dumps(wrapper))
        self.assertEqual(inspect(self.archive, self.receipt)['owner'], self.owner)
        with self.assertRaisesRegex(ValueError, 'different desktop'):
            inspect(self.archive, self.receipt, owner=str(uuid4()))
        wrapper['uuid'] = str(uuid4())
        self.receipt.write_text(json.dumps(wrapper))
        with self.assertRaisesRegex(ValueError, 'different desktops'):
            inspect(self.archive, self.receipt)

    def test_changed_bytes_rejected_even_when_size_is_unchanged(self):
        data = bytearray(self.archive.read_bytes())
        data[12] ^= 1
        self.archive.write_bytes(data)
        with self.assertRaisesRegex(ValueError, 'digest differs'):
            inspect(self.archive, self.receipt, find='trace.zip')

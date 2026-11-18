"""Inspect the exact Windows archive saved by desktop archive or collect."""
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import uuid
import zipfile


def inspect(archive, receipt_path, *, owner=None, find=None):
    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    if not isinstance(receipt, dict):
        raise ValueError('Expected a WinHoo archive receipt')
    if 'archive' in receipt:
        nested = receipt['archive']
        if not isinstance(nested, dict) or receipt.get('uuid') != nested.get('owner'):
            raise ValueError('Collection and archive identify different desktops')
        receipt = nested
    recorded_owner = str(uuid.UUID(str(receipt.get('owner'))))
    size = receipt.get('bytes')
    digest = receipt.get('sha256')
    if owner is not None and str(uuid.UUID(owner)) != recorded_owner:
        raise ValueError('Archive belongs to a different desktop')
    if type(size) is not int or size < 1 or not re.fullmatch('[0-9a-f]{64}', str(digest)):
        raise ValueError('Expected byte count and SHA-256 in the archive receipt')
    if find is not None and (not find or '/' in find or '\\' in find or find in ('.', '..')):
        raise ValueError('--find requires a filename without a directory')

    with archive.open('rb') as stream:
        before = os.fstat(stream.fileno())
        if before.st_size != size:
            raise ValueError('Saved file size differs from the guest archive receipt')
        actual = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            actual.update(chunk)
        if actual.hexdigest() != digest:
            raise ValueError('Saved file digest differs from the guest archive receipt')
        try:
            with zipfile.ZipFile(stream) as bundle:
                files = [entry for entry in bundle.infolist() if not entry.is_dir()]
        except zipfile.BadZipFile as exc:
            raise ValueError('The recorded file is not a readable ZIP archive') from exc
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('Archive changed during inspection')
    result = {'owner': recorded_owner, 'bytes': size, 'sha256': digest, 'files': len(files)}
    if find is not None:
        matches = [{'path': entry.filename, 'bytes': entry.file_size} for entry in files
                   if PurePosixPath(entry.filename).name == find]
        if not matches:
            raise ValueError('No matching filename in the verified archive')
        result['matches'] = matches
    return result

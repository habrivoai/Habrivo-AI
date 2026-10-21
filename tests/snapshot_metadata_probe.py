"""Privacy-safe Windows/POSIX metadata investigation on one new synthetic file.

Not a snapshot validator or a change to its metadata policy. No SQLite connection,
application import, caller-supplied source, credentials or customer records.
"""
import json
import os
from pathlib import Path
import sys
import tempfile

FIELDS = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')


def diagnose():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'synthetic-metadata.bin'
        contents = bytes(range(256)) * 16
        with path.open('xb') as stream:
            stream.write(contents)
        before = path.lstat()
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0))
        try:
            opened = os.fstat(fd)
            chunks = []
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                chunks.append(chunk)
            final_fd = os.fstat(fd)
            final_path = path.lstat()
            return {
                'diagnostic_only': True, 'platform': sys.platform,
                'path_vs_descriptor': {name: getattr(before,name) == getattr(opened,name) for name in FIELDS},
                'descriptor_stable': {name: getattr(opened,name) == getattr(final_fd,name) for name in FIELDS},
                'path_stable': {name: getattr(before,name) == getattr(final_path,name) for name in FIELDS},
                'content_unchanged': b''.join(chunks) == contents == path.read_bytes(),
            }
        finally:
            os.close(fd)


if __name__ == '__main__':
    try:
        print(json.dumps(diagnose(), sort_keys=True))
    except Exception:
        print(json.dumps({'diagnostic_only':True,'error':'synthetic_metadata_probe_failed'}))
        raise SystemExit(1)

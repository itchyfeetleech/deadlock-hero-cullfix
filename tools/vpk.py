"""Read-only Valve VPK inventory/extraction with CRC verification."""
import struct
import zlib
from pathlib import Path

class VPK:
    def __init__(self, path):
        self.path = Path(path)
        self.entries = {}
        with self.path.open('rb') as f:
            magic, version, size = struct.unpack('<III', f.read(12))
            assert magic == 0x55aa1234 and version in (1, 2)
            header = 28 if version == 2 else 12
            self.data_start = header + size
            f.seek(header)
            def string():
                b = bytearray()
                while (c := f.read(1)) != b'\0':
                    if not c:
                        raise EOFError('Truncated VPK')
                    b += c
                return b.decode()
            while ext := string():
                while folder := string():
                    while name := string():
                        crc, preload, archive, offset, length, end = struct.unpack('<IHHIIH', f.read(18))
                        assert end == 65535
                        key = (folder + '/' if folder != ' ' else '') + name + '.' + ext
                        self.entries[key] = (crc, f.read(preload), archive, offset, length)

    def read(self, name):
        crc, pre, archive, offset, length = self.entries[name]
        path = self.path if archive == 0x7fff else self.path.with_name(self.path.name.replace('_dir.vpk', f'_{archive:03}.vpk'))
        with path.open('rb') as f:
            f.seek(offset + (self.data_start if archive == 0x7fff else 0))
            data = pre + f.read(length)
        assert zlib.crc32(data) == crc, name
        return data

    def extract(self, name, dest):
        out = Path(dest) / name
        assert out.resolve().is_relative_to(Path(dest).resolve())
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(self.read(name))
        return out

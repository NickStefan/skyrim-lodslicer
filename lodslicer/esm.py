"""Read and write Skyrim SE plugins (ESM/ESP/ESL) without extra modules."""
import struct
import zlib

COMPRESSED = 0x40000
ESL = 0x200
FORM_VERSION = 44
HEADER_VERSION = 1.70  # 1.71 needs AE; SE 1.5.97 only loads ESLs up to 1.70 (ids 0x800-0xFFF)

# Group types
WORLD_CHILDREN = 1
CELL_PERSISTENT = 8
CELL_TEMPORARY = 9


def text(value):
    return value.rstrip(b'\0').decode('cp1252', 'replace')


def subrecords(data):
    at, size_override = 0, None
    while at + 6 <= len(data):
        kind, size = data[at:at + 4].decode('ascii', 'replace'), struct.unpack_from('<H', data, at + 4)[0]
        at += 6
        if kind == 'XXXX':
            size_override = struct.unpack_from('<I', data, at)[0]
            at += size
            continue
        if size_override is not None:
            size, size_override = size_override, None
        yield kind, data[at:at + size]
        at += size


def header(data):
    """(masters, offset of the first top-level group) of a whole plugin file."""
    size = struct.unpack_from('<I', data, 4)[0]
    masters = [text(v) for k, v in subrecords(data[24:24 + size]) if k == 'MAST']
    return masters, 24 + size


def walk(data, at, end, path=()):
    """Yields (type, flags, form id, body, group path) for each record; the path is the enclosing groups'
    (group type, label bytes), outermost first. Compressed bodies are decompressed."""
    while at + 24 <= end:
        kind = data[at:at + 4].decode('ascii', 'replace')
        size = struct.unpack_from('<I', data, at + 4)[0]
        if kind == 'GRUP':
            label = data[at + 8:at + 12]
            group_type = struct.unpack_from('<i', data, at + 12)[0]
            yield from walk(data, at + 24, at + size, path + ((group_type, label),))
            at += size
            continue
        flags, form_id = struct.unpack_from('<II', data, at + 8)
        body = data[at + 24:at + 24 + size]
        if flags & COMPRESSED:
            body = zlib.decompress(body[4:])
        yield kind, flags, form_id, body, path
        at += 24 + size


def top_group(data, start, label):
    """(start, end) of the top-level group with this label, or None."""
    at = start
    while at < len(data):
        size = struct.unpack_from('<I', data, at + 4)[0]
        if data[at + 8:at + 12] == label:
            return at, at + size
        at += size
    return None


def zstring(value):
    return value.encode('cp1252') + b'\0'


def sub(kind, data):
    return kind.encode() + struct.pack('<H', len(data)) + data


def record(kind, form_id, *subs, flags=0):
    data = b''.join(subs)
    return kind.encode() + struct.pack('<IIIIHH', len(data), flags, form_id, 0, FORM_VERSION, 0) + data


def group(label, *contents, group_type=0):
    """A group; label is a record type for top-level groups, or packed bytes (form id, grid) for others."""
    data = b''.join(contents)
    label = label.encode() if isinstance(label, str) else label
    return b'GRUP' + struct.pack('<I4siHHI', 24 + len(data), label, group_type, 0, 0, 0) + data

#!/usr/bin/env python3
"""Write the LOD generation patch described by targets.json (run analyze.py first).

    python3 make_patch.py [CONFIG]     default lodslicer.json; writes patch/<config "patch" name>

The patch is an ESL-flagged plugin for generating object LOD only; never play with it. It:
- overrides the chosen base STATs with the Has Distant LOD flag and LOD models (MNAM),
- overrides their references in the plugins with the Visible When Distant flag,
- applies the plugins' Base Object Swapper transforms (position, flags) to the references they change.
Reference overrides keep only what LOD generation reads (base, placement, scale, enable parent, flags), so
links, ownership and the like are dropped. Load it last, generate, then remove it.
"""
import json
import struct
import sys
from pathlib import Path

from lodslicer import esm
from lodslicer.esm import CELL_PERSISTENT, CELL_TEMPORARY, WORLD_CHILDREN, sub, subrecords, zstring
from lodslicer.records import LOD_SLOT, VISIBLE_WHEN_DISTANT, Plugin, load_statics, references

EXTERIOR_BLOCK = 4
EXTERIOR_SUB_BLOCK = 5
CELL_CHILDREN = 6
COMPRESSED = esm.COMPRESSED
CELL_KEEP = ('EDID', 'DATA', 'XCLC', 'XCLW', 'XCWT')
REF_KEEP = ('EDID', 'NAME', 'XSCL', 'XESP', 'DATA')


def parse_key(text):
    plugin, object_id = text.rsplit(':', 1)
    return plugin, int(object_id, 16)


class Patch:
    def __init__(self, masters):
        self.masters = masters
        self.index = {m.lower(): i for i, m in enumerate(masters)}

    def form_id(self, key):
        return (self.index[key[0]] << 24) | key[1]

    def same_indices(self, plugin):
        """True if the plugin's form ids mean the same in the patch, so its record bodies copy unchanged."""
        return all(self.index.get(f.lower()) == i for i, f in enumerate(plugin.files))

    def remap(self, plugin, raw):
        return struct.pack('<I', self.form_id(plugin.key(struct.unpack_from('<I', raw)[0]))) + raw[4:]


def count_entries(data, at=0, end=None):
    """Records and groups in a run of top-level groups, as the TES4 header's record count expects."""
    end = len(data) if end is None else end
    count = 0
    while at < end:
        size = struct.unpack_from('<I', data, at + 4)[0]
        if data[at:at + 4] == b'GRUP':
            count += 1 + count_entries(data, at + 24, at + size)
            at += size
        else:
            count += 1
            at += 24 + size
    return count


def winning_records(data_dir, order, kinds, keys):
    """{key: [(plugin, flags, body), ...]} of every version of each wanted WRLD/CELL record, in load order."""
    found = {}
    for name in order:
        plugin = Plugin(data_dir, name)
        for kind, flags, form_id, body, _ in plugin.group(b'WRLD'):
            if kind in kinds and plugin.key(form_id) in keys:
                found.setdefault(plugin.key(form_id), []).append((plugin, flags, body))
    return found


def copyable(patch, versions, what):
    """The winning version if its form ids mean the same in the patch, otherwise the latest version that does
    and has identical content (excluding OFST), so it can be copied unchanged."""
    def content(body):
        return [(k, v) for k, v in subrecords(body) if k != 'OFST']

    plugin, flags, body = versions[-1]
    if patch.same_indices(plugin):
        return versions[-1]
    for candidate in reversed(versions[:-1]):
        if patch.same_indices(candidate[0]) and content(candidate[2]) == content(body) and candidate[1] == flags:
            return candidate
    raise SystemExit(f'{what} wins from {plugin.name}, whose masters differ from the patch, and no earlier '
                     'identical version can stand in for it')


def keep(body, kinds, plugin, patch, form_id_fields=()):
    out = b''
    for kind, value in subrecords(body):
        if kind in kinds:
            if kind in form_id_fields:
                value = patch.remap(plugin, value)
            out += sub(kind, value)
    return out


def static_override(patch, base, plugin, lod):
    if not patch.same_indices(plugin):
        raise SystemExit(f'{base["editor_id"]} comes from {plugin.name}, whose masters differ from the patch; '
                         'copying it needs form id remapping')
    body = b''.join(sub(k, v) for k, v in subrecords(base['body']) if k != 'MNAM')
    mnam = b''.join(path.encode('cp1252').ljust(LOD_SLOT, b'\0') for path in lod)
    flags = (base['flags'] | VISIBLE_WHEN_DISTANT) & ~COMPRESSED
    return esm.record('STAT', base['form_id'], body + sub('MNAM', mnam), flags=flags)


def world_override(flags, form_id, body):
    # OFST is the master's cell offset table; it means nothing in another file.
    kept = b''.join(sub(k, v) for k, v in subrecords(body) if k != 'OFST')
    return esm.record('WRLD', form_id, kept, flags=flags & ~COMPRESSED)


def main():
    config_path = Path(sys.argv[1] if len(sys.argv) > 1 else 'lodslicer.json').resolve()
    root = config_path.parent
    config = json.loads(config_path.read_text())
    targets = json.loads((root / 'targets.json').read_text())
    data = (root / config['data']).resolve()
    order = targets['order']
    patch = Patch(order)

    statics = load_statics(data, order)
    lod_by_base = {parse_key(k): b['lod'] for k, b in targets['bases'].items()}
    vwd = {parse_key(r['key']) for r in targets['patch_refs']}
    swaps = {parse_key(s['key']): s['values'] for s in targets['swaps']}

    refs = []
    for name in config['plugins']:
        refs += [r for r in references(data, name, keys=vwd | set(swaps))]
    worlds = {r['world'][0] for r in refs}
    cells = {r['cell'] for r in refs}
    winners = winning_records(data, order, ('WRLD', 'CELL'), worlds | cells)

    plugins = {name: Plugin(data, name) for name in {r['source'] for r in refs}}
    tree = {}  # world -> persistent cell or (block, sub-block) -> cell -> [records]
    for r in refs:
        plugin = plugins[r['source']]
        fields = {k: v for k, v in subrecords(r['body'])}
        flags = r['flags'] & ~COMPRESSED
        if r['key'] in vwd:
            flags |= VISIBLE_WHEN_DISTANT
        values = swaps.get(r['key'], {})
        if values:
            x, y, z, rx, ry, rz = struct.unpack('<6f', fields['DATA'])
            if 'posA' in values:
                x, y, z = values['posA']
            if 'posR' in values:
                x, y, z = x + values['posR'][0], y + values['posR'][1], z + values['posR'][2]
            fields['DATA'] = struct.pack('<6f', x, y, z, rx, ry, rz)
            flags |= values.get('flags', 0)
        body = b''
        for kind in REF_KEEP:
            if kind in fields:
                value = fields[kind]
                body += sub(kind, patch.remap(plugin, value) if kind in ('NAME', 'XESP') else value)
        record = esm.record('REFR', patch.form_id(r['key']), body, flags=flags)
        world = tree.setdefault(r['world'][0], {})
        if r['persistent']:
            place = 'persistent'
        else:
            gx, gy = r['grid']
            place = ((gx // 32, gy // 32), (gx // 8, gy // 8))
        world.setdefault(place, {}).setdefault(r['cell'], []).append(record)

    def cell_record(key):
        plugin, flags, body = winners[key][-1]
        return esm.record('CELL', patch.form_id(key), keep(body, CELL_KEEP, plugin, patch, ('XCWT',)),
                          flags=flags & ~COMPRESSED)

    def cell_group(key, records, children_type):
        label = struct.pack('<I', patch.form_id(key))
        return cell_record(key) + esm.group(label, esm.group(label, *records, group_type=children_type),
                                            group_type=CELL_CHILDREN)

    world_groups = []
    for world_key, places in tree.items():
        plugin, flags, body = copyable(patch, winners[world_key], f'worldspace {world_key}')
        contents = []
        for key, records in places.get('persistent', {}).items():
            contents.append(cell_group(key, records, CELL_PERSISTENT))
        blocks = {}
        for place, cell_map in places.items():
            if place != 'persistent':
                blocks.setdefault(place[0], {}).setdefault(place[1], cell_map)
        for (bx, by), sub_blocks in sorted(blocks.items()):
            inner = []
            for (sx, sy), cell_map in sorted(sub_blocks.items()):
                inner.append(esm.group(struct.pack('<hh', sy, sx),
                                       *(cell_group(k, recs, CELL_TEMPORARY) for k, recs in cell_map.items()),
                                       group_type=EXTERIOR_SUB_BLOCK))
            contents.append(esm.group(struct.pack('<hh', by, bx), *inner, group_type=EXTERIOR_BLOCK))
        world_groups.append(world_override(flags, patch.form_id(world_key), body) +
                            esm.group(struct.pack('<I', patch.form_id(world_key)), *contents,
                                      group_type=WORLD_CHILDREN))

    statics_out = [static_override(patch, statics[key], Plugin(data, statics[key]['source']), lod)
                   for key, lod in lod_by_base.items()]
    groups = [esm.group('STAT', *statics_out), esm.group('WRLD', *world_groups)]
    body = b''.join(groups)
    header_subs = [sub('HEDR', struct.pack('<fiI', esm.HEADER_VERSION, count_entries(body), 0x800)),
                   sub('CNAM', zstring('lodslicer')),
                   sub('SNAM', zstring('LOD generation only. Remove after generating LOD; do not play with it.'))]
    for master in order:
        header_subs += [sub('MAST', zstring(master)), sub('DATA', struct.pack('<Q', 0))]
    out = root / 'patch' / config['patch']
    out.parent.mkdir(exist_ok=True)
    out.write_bytes(esm.record('TES4', 0, *header_subs, flags=esm.ESL) + body)

    verify(out, patch, lod_by_base, vwd, swaps, refs)
    print(f'wrote {out} ({out.stat().st_size // 1024} KB): {len(statics_out)} statics, '
          f'{len(vwd & {r["key"] for r in refs})} references made visible when distant, '
          f'{len(swaps)} swap transforms, masters {", ".join(order)}')
    return 0


def verify(path, patch, lod_by_base, vwd, swaps, refs):
    """Read the patch back and check every override resolves to what it should."""
    written = Plugin(path.parent, path.name)
    assert [f.lower() for f in written.files[:-1]] == [m.lower() for m in patch.masters]
    expected_base = {r['key']: r['base'] for r in refs}
    seen_statics, seen_refs = set(), set()
    for _, flags, form_id, body, _ in written.group(b'STAT'):
        key = written.key(form_id)
        fields = dict(subrecords(body))
        assert flags & VISIBLE_WHEN_DISTANT and key in lod_by_base, key
        paths = [fields['MNAM'][i * LOD_SLOT:(i + 1) * LOD_SLOT].split(b'\0')[0].decode() for i in range(4)]
        assert paths == lod_by_base[key], (key, paths)
        seen_statics.add(key)
    for kind, flags, form_id, body, path in written.group(b'WRLD'):
        if kind != 'REFR':
            continue
        key = written.key(form_id)
        fields = dict(subrecords(body))
        assert written.key(struct.unpack('<I', fields['NAME'])[0]) == expected_base[key], key
        assert bool(flags & VISIBLE_WHEN_DISTANT) == (key in vwd) or key in swaps, key
        if key in swaps and 'posA' in swaps[key]:
            assert abs(struct.unpack_from('<3f', fields['DATA'])[2] - swaps[key]['posA'][2]) < 1e-3, key
        seen_refs.add(key)
    assert seen_statics == set(lod_by_base), 'missing statics'
    assert seen_refs == set(expected_base), 'missing references'


if __name__ == '__main__':
    sys.exit(main())

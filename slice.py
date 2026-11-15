#!/usr/bin/env python3
"""Keep the target blocks of xLODGen's object LOD output, give its atlas a name of its own, check the result
and package it.

    python3 slice.py [CONFIG]     default lodslicer.json; reads targets.json and the xLODGen output folder

Writes result/<package>.bsa (uncompressed: the blocks and the renamed atlas) and result/<package>.esp (an
empty ESL-flagged plugin that makes the game load the archive).

Object LOD alpha tests every texture at a fixed threshold of 128, even where the full model ignores the
alpha channel. Textures listed in the config's "opaque_textures" are copied without alpha to
textures\\lod\\<atlas>\\ and the blocks point there instead.
"""
import json
import math
import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path

from lodslicer import bsa, dds, esm
from lodslicer.esm import sub, zstring
from lodslicer.nif import retexture, shapes
from lodslicer.records import DELETED, INITIALLY_DISABLED, archives, load_statics, plugin_order, references

OBJECTS = 'meshes\\terrain\\tamriel\\objects\\'
ATLAS_DIR = 'textures\\terrain\\tamriel\\objects\\'
BIN = 128  # units: spatial bins for finding geometry near a reference
COVERAGE_BIN = 256
GONE_Z = -29000
WATER = -14000  # Tamriel's default water height; xLODGen drops object LOD triangles below it


def bins(points, size):
    found = defaultdict(list)
    for x, y, z in points:
        found[(math.floor(x / size), math.floor(y / size))].append(z)
    return found


def near(grid, x, y, z, radius, height):
    """Vertices within radius (in x, y) and height (in z) of a point, from bins of BIN units."""
    reach = math.ceil(radius / BIN)
    bx, by = math.floor(x / BIN), math.floor(y / BIN)
    return sum(1 for dx in range(-reach, reach + 1) for dy in range(-reach, reach + 1)
               for vz in grid.get((bx + dx, by + dy), ()) if abs(vz - z) <= height)


def rename_atlas(data, old, new):
    """Replace the atlas name inside length-prefixed texture paths; the names have the same length."""
    count = 0
    for suffix in (b'.dds', b'_n.dds'):
        needle = old + suffix
        count += data.count(needle)
        data = data.replace(needle, new + suffix)
    return data, count


def main():
    config_path = Path(sys.argv[1] if len(sys.argv) > 1 else 'lodslicer.json').resolve()
    root = config_path.parent
    config = json.loads(config_path.read_text())
    targets = json.loads((root / 'targets.json').read_text())
    data = (root / config['data']).resolve()
    source = root / config['xlodgen_output']
    old_atlas, new_atlas = config['xlodgen_atlas'].lower().encode(), config['atlas'].lower().encode()
    if len(old_atlas) != len(new_atlas):
        raise SystemExit(f'atlas name must have {len(old_atlas)} characters to replace {old_atlas.decode()} in place')

    meshes = {p.name.lower(): p for p in (source / 'meshes/terrain/Tamriel/Objects').iterdir()}
    textures = {p.name.lower(): p for p in (source / 'textures/terrain/Tamriel/Objects').iterdir()}
    vanilla = [bsa.Archive(p) for p in sorted(data.glob('Skyrim - Meshes*.bsa'))]

    files, blocks, problems = {}, {}, []
    opaque = {}
    if config.get('opaque_textures'):
        loaded, _ = archives(data, plugin_order(data, config))
        by_priority = [(p.name, bsa.Archive(p)) for p in reversed(loaded)]
        for texture in config['opaque_textures']:
            texture = texture.lower()
            source_name, original = next(((n, a.read(texture)) for n, a in by_priority if texture in a.files),
                                         (None, None))
            if original is None:
                raise SystemExit(f'{texture} (in opaque_textures) is not in any archive the load order loads')
            target = f'textures\\lod\\{new_atlas.decode()}\\{texture.rsplit(chr(92), 1)[1]}'
            files[target] = dds.opaque(original, config.get('opaque_max_size'))
            width = struct.unpack_from('<I', files[target], 16)[0]
            print(f'opaque copy of {texture} from {source_name}, {width}px')
            opaque['data\\' + texture] = 'Data\\' + target
    for level, cells in targets['blocks'].items():
        for bx, by in cells:
            name = f'tamriel.{level}.{bx}.{by}.bto'
            if name not in meshes:
                if any(OBJECTS + name in a.files for a in vanilla):
                    problems.append(f'{name}: vanilla has this block but xLODGen did not write it')
                continue
            content, count = rename_atlas(meshes[name].read_bytes(), b'objects\\' + old_atlas,
                                          b'objects\\' + new_atlas)
            if b'tamriel.objects' in content.lower():
                problems.append(f'{name}: still references the vanilla atlas')
            content, repointed = retexture(content, lambda p: opaque.get(p.lower(), p))
            if list(shapes(content)) != list(shapes(meshes[name].read_bytes())):
                problems.append(f'{name}: geometry changed while swapping textures')
            files[OBJECTS + name] = content
            blocks[(int(level), bx, by)] = content
            print(f'kept {name:24} {len(content):8d} bytes, atlas references renamed: {count}'
                  + (f', opaque textures: {repointed}' if repointed else ''))
    for suffix in ('.dds', '_n.dds'):
        texture = textures.get(old_atlas.decode() + suffix)
        if not texture:
            raise SystemExit(f'atlas texture {old_atlas.decode()}{suffix} not found in the xLODGen output')
        files[ATLAS_DIR + new_atlas.decode() + suffix] = texture.read_bytes()

    print('\nchecking the target references against the LOD4 blocks...')
    geometry = {key: bins([p for _, pts in shapes(content) for p in pts], BIN) for key, content in blocks.items()}
    statics = load_statics(data, targets['order'])
    swapped = {s['key'] for s in targets['swaps']}
    chosen = set(targets['bases'])
    found, missing, leaked, underwater = Counter(), [], [], 0
    for name in config['plugins']:
        for r in references(data, name):
            base = statics.get(r['base'])
            if not base or r['flags'] & DELETED:
                continue
            key = f'{r["key"][0]}:{r["key"][1]:06X}'
            base_key = f'{r["base"][0]}:{r["base"][1]:06X}'
            expected = base_key in chosen or any(base['lod'])
            gone = key in swapped or r['flags'] & INITIALLY_DISABLED or r['position'][2] <= GONE_Z
            if not (expected or (key in swapped and (base_key in chosen or any(base['lod'])))):
                continue
            grid = r['grid']
            block = (4, grid[0] // 4 * 4, grid[1] // 4 * 4)
            if block not in geometry:
                continue
            size = base['size'] * r['scale']
            x, y, z = r['position']
            hits = near(geometry[block], x, y, z, max(150, min(600, 0.3 * size)), max(200, size))
            if gone:
                if hits:
                    leaked.append((key, base['editor_id'], hits))
            elif hits:
                found[base['editor_id']] += 1
            elif z + size / 2 < WATER:
                underwater += 1
            else:
                missing.append((key, base['editor_id'], round(size), tuple(round(v) for v in r['position'])))
    print(f'  {sum(found.values())} references have LOD4 geometry at their position')
    print(f'  {underwater} references without geometry are under water')
    for key, editor_id, size, position in missing:
        problems.append(f'no LOD4 geometry near {key} {editor_id} (size {size}) at {position}')
    for key, editor_id, hits in leaked:
        print(f'  note: {hits} vertices near removed reference {key} {editor_id} (could be neighbouring objects)')

    print('\ncomparing coverage above water with vanilla blocks...')
    for (level, bx, by), content in sorted(blocks.items()):
        name = f'tamriel.{level}.{bx}.{by}.bto'
        archive = next((a for a in vanilla if OBJECTS + name in a.files), None)
        if not archive:
            print(f'  {name:24} no vanilla block')
            continue
        old = bins([p for _, pts in shapes(archive.read(OBJECTS + name)) for p in pts if p[2] > WATER + 50],
                   COVERAGE_BIN)
        new = bins([p for _, pts in shapes(content) for p in pts if p[2] > WATER + 50], COVERAGE_BIN)
        lost = [b for b in old if not any((b[0] + dx, b[1] + dy) in new for dx in (-1, 0, 1) for dy in (-1, 0, 1))]
        gained = [b for b in new if not any((b[0] + dx, b[1] + dy) in old for dx in (-1, 0, 1) for dy in (-1, 0, 1))]
        line = f'  {name:24} vanilla areas {len(old):4d}, lost {len(lost):3d}, new areas {len(gained):4d}'
        if lost:
            centres = [((b[0] + 0.5) * COVERAGE_BIN, (b[1] + 0.5) * COVERAGE_BIN) for b in lost]
            line += f'  lost near ' + ', '.join(f'({x:.0f},{y:.0f})' for x, y in centres[:4])
            line += ' ...' if len(centres) > 4 else ''
        print(line)

    result = root / 'result'
    result.mkdir(exist_ok=True)
    archive_path = result / f'{config["package"]}.bsa'
    plugin_path = result / f'{config["package"]}.esp'
    bsa.write(archive_path, files)
    header = [sub('HEDR', struct.pack('<fiI', esm.HEADER_VERSION, 0, 0x800)), sub('CNAM', zstring('lodslicer')),
              sub('SNAM', zstring('Loads the object LOD archive of the same name.')),
              sub('MAST', zstring('Skyrim.esm')), sub('DATA', struct.pack('<Q', 0))]
    plugin_path.write_bytes(esm.record('TES4', 0, *header, flags=esm.ESL))

    written = bsa.Archive(archive_path)
    for name, content in files.items():
        assert written.read(name) == content, name
    print(f'\nwrote {archive_path.name} ({archive_path.stat().st_size // 1024} KB, {len(files)} files) and '
          f'{plugin_path.name} in {result}')
    if problems:
        print(f'\n{len(problems)} problems:')
        for p in problems:
            print('  ' + p)
        return 1
    print('no problems found')
    return 0


if __name__ == '__main__':
    sys.exit(main())

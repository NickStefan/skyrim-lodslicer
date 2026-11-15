"""What lodslicer needs from a load order: base statics, placed references, swap files and LOD models."""
import math
import re
import struct
from collections import defaultdict
from pathlib import Path

from .bsa import Archive
from .esm import CELL_PERSISTENT, header, subrecords, text, top_group, walk

CELL_SIZE = 4096
ESM = 0x1
DELETED = 0x20
INITIALLY_DISABLED = 0x800
VISIBLE_WHEN_DISTANT = 0x8000  # on a STAT: Has Distant LOD
LOD_SLOT = 260  # MNAM: four LOD model paths of 260 bytes each, for LOD levels 4, 8, 16, 32
OFFICIAL = ['skyrim.esm', 'update.esm', 'dawnguard.esm', 'hearthfires.esm', 'dragonborn.esm']
# Skyrim.ini's sResourceArchiveList and sResourceArchiveList2; plugins' archives load after these
VANILLA_ARCHIVES = ['skyrim - misc.bsa', 'skyrim - shaders.bsa', 'skyrim - interface.bsa', 'skyrim - animations.bsa',
                    'skyrim - meshes0.bsa', 'skyrim - meshes1.bsa', 'skyrim - sounds.bsa', 'skyrim - voices_en0.bsa',
                    *(f'skyrim - textures{i}.bsa' for i in range(9)), 'skyrim - patch.bsa']
LOD_NAME = re.compile(r'^meshes\\(?:.*\\)?lod\\(?:.*\\)?([^\\]+)_lod_([0-3])\.nif$')


def is_esm(path):
    return bool(struct.unpack_from('<I', path.read_bytes()[:12], 8)[0] & ESM)


def load_order(data_dir, plugins):
    """The plugins and their masters, masters first, ESM-flagged files before the rest as the game loads them."""
    order = []

    def visit(name):
        if name.lower() in (n.lower() for n in order):
            return
        masters, _ = header((data_dir / name).read_bytes())
        for master in masters:
            visit(master)
        order.append(name)

    for plugin in plugins:
        visit(plugin)

    def rank(name):
        official = OFFICIAL.index(name.lower()) if name.lower() in OFFICIAL else len(OFFICIAL)
        return official, not (is_esm(data_dir / name) or name.lower().endswith('.esm')), order.index(name)

    return sorted(order, key=rank)


def plugin_order(data_dir, config):
    """The plugins whose archives the game loads, in load order: config "load_order" (names from your real
    load order; the files needn't be in DATA), with the job's masters placed before it and the job's plugins
    after it unless it names them."""
    listed = config.get('load_order', [])
    named = {n.lower() for n in listed}
    jobs = {n.lower() for n in config['plugins']}
    order = [n for n in load_order(data_dir, list(config['plugins'])) if n.lower() not in named]
    return ([n for n in order if n.lower() not in jobs] + listed + [n for n in order if n.lower() in jobs])


def archives(data_dir, plugins):
    """(archives in DATA the game loads, lowest priority first; archives in DATA nothing loads). The game
    loads the vanilla archives, then <plugin>.bsa and <plugin> - Textures.bsa for each plugin in load order,
    each overriding the files of those before it."""
    present = {p.name.lower(): p for p in data_dir.iterdir() if p.suffix.lower() == '.bsa'}
    names = list(VANILLA_ARCHIVES)
    for plugin in plugins:
        stem = Path(plugin).stem.lower()
        names += [f'{stem}.bsa', f'{stem} - textures.bsa']
    loaded = list(dict.fromkeys(n for n in names if n in present))
    return [present[n] for n in loaded], sorted(present[n] for n in present if n not in loaded)


class Plugin:
    def __init__(self, data_dir, name):
        self.name = name
        self.data = (data_dir / name).read_bytes()
        masters, self.start = header(self.data)
        self.files = masters + [name]

    def key(self, form_id):
        """(owning plugin in lower case, object id) of a form id as written in this plugin."""
        return self.files[min(form_id >> 24, len(self.files) - 1)].lower(), form_id & 0xFFFFFF

    def group(self, label):
        span = top_group(self.data, self.start, label)
        return walk(self.data, span[0] + 24, span[1]) if span else iter(())


def lod_paths(mnam):
    return [text(mnam[i * LOD_SLOT:(i + 1) * LOD_SLOT].split(b'\0')[0]) for i in range(len(mnam) // LOD_SLOT)]


def load_statics(data_dir, order):
    """{key: winning STAT} with its source plugin, raw form id, flags, body and parsed fields."""
    statics = {}
    for name in order:
        plugin = Plugin(data_dir, name)
        for _, flags, form_id, body, _ in plugin.group(b'STAT'):
            fields = list(subrecords(body))
            bounds = next((struct.unpack('<6h', v) for k, v in fields if k == 'OBND'), (0,) * 6)
            statics[plugin.key(form_id)] = dict(
                source=name, form_id=form_id, flags=flags, body=body,
                editor_id=next((text(v) for k, v in fields if k == 'EDID'), ''),
                model=next((text(v) for k, v in fields if k == 'MODL'), ''),
                lod=lod_paths(next((v for k, v in fields if k == 'MNAM'), b'')),
                size=max(bounds[3] - bounds[0], bounds[4] - bounds[1], bounds[5] - bounds[2]))
    return statics


def references(data_dir, name, keys=None):
    """Each REFR in a plugin's worldspaces (only those whose key is in keys, if given), with its placement,
    world, cell grid, raw body and group path."""
    plugin = Plugin(data_dir, name)
    world = cell = None
    for kind, flags, form_id, body, path in plugin.group(b'WRLD'):
        if kind == 'WRLD':
            world = (plugin.key(form_id), text(next((v for k, v in subrecords(body) if k == 'EDID'), b'')))
            continue
        if kind == 'CELL':
            cell = plugin.key(form_id)
            continue
        if kind != 'REFR' or (keys is not None and plugin.key(form_id) not in keys):
            continue
        fields = {}
        for k, v in subrecords(body):
            fields.setdefault(k, v)
        if 'DATA' not in fields:
            continue
        x, y, z, rx, ry, rz = struct.unpack('<6f', fields['DATA'])
        yield dict(
            source=name, form_id=form_id, key=plugin.key(form_id), flags=flags, body=body,
            new=(form_id >> 24) == len(plugin.files) - 1, world=world, cell=cell,
            base=plugin.key(struct.unpack('<I', fields['NAME'])[0]),
            position=(x, y, z), rotation=(rx, ry, rz),
            scale=struct.unpack('<f', fields['XSCL'])[0] if 'XSCL' in fields else 1.0,
            grid=(math.floor(x / CELL_SIZE), math.floor(y / CELL_SIZE)),
            persistent=any(g == CELL_PERSISTENT for g, _ in path))


def lod_models(paths):
    """{model file name without extension: {level: path relative to meshes\\}} from archives listed lowest
    priority first, as archives() returns them."""
    found = defaultdict(dict)
    for archive in paths:
        for name in Archive(archive).files:
            match = LOD_NAME.match(name)
            if match:
                found[match.group(1)][int(match.group(2))] = name[len('meshes\\'):]
    return found


def model_stem(model):
    return Path(model.replace('\\', '/')).stem.lower()


SWAP_LINE = re.compile(r'^\s*(?:0x)?([0-9A-Fa-f]+)~([^|]+)\|(.*)$')
TRANSFORM = re.compile(r'(\w+)\(([^)]*)\)')


def swaps(data_dir):
    """Base Object Swapper transforms from every *_SWAP.ini in DATA, in the alphabetical order BOS reads them:
    [(file, (plugin, object id), {'posA': (x, y, z), 'flags': n, ...})]. Other sections are returned with
    their lines so the caller can report them as unsupported."""
    transforms, unsupported = [], []
    for path in sorted(data_dir.glob('*_SWAP.ini')):
        section = None
        for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
            line = line.split(';', 1)[0].strip()
            if not line:
                continue
            if line.startswith('['):
                section = line.strip('[]').lower()
                continue
            match = SWAP_LINE.match(line)
            if section != 'transforms' or not match:
                unsupported.append((path.name, section, line))
                continue
            values = {}
            for name, args in TRANSFORM.findall(match.group(3)):
                parts = [a.strip() for a in args.split(',')]
                values[name] = int(parts[0], 16) if name == 'flags' else tuple(float(p) for p in parts)
            transforms.append((path.name, (match.group(2).strip().lower(), int(match.group(1), 16) & 0xFFFFFF),
                               values))
    return transforms, unsupported


def lod_plan(available):
    """MNAM paths for LOD levels 4, 8, 16, 32 from the LOD models found for a model: the most detailed model
    for LOD4, then each coarser level uses its own model, LOD8 falling back to the LOD4 one only if that was
    a level 1 model."""
    level0 = available.get(0) or available.get(1) or ''
    level1 = available.get(1) or ''
    level2 = available.get(2) or ''
    return [level0, level1, level2, '']

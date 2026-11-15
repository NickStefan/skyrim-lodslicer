#!/usr/bin/env python3
"""Decide what the LOD generation patch changes and which LOD blocks to keep afterwards.

    python3 analyze.py [CONFIG]     default lodslicer.json; writes targets.json next to it

CONFIG names the data folder (plugins, masters, mesh archives, *_SWAP.ini files) and, per plugin, which
references to give object LOD: those whose base STAT has no LOD models (MNAM) but has a LOD model named
<model>_lod_<level>.nif under meshes\\lod\\, whose model path matches "models" (a regular expression) and
whose size after scaling is at least "min_size" units. LOD models belong to the base record, so one
qualifying reference gives every reference of that base LOD.

The blocks to keep cover every cell holding a reference of the plugins, and the cells the plugins moved
vanilla references out of.
"""
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from lodslicer.records import (DELETED, INITIALLY_DISABLED, Plugin, archives, load_order, load_statics, lod_models,
                               lod_plan, model_stem, plugin_order, references, swaps)

LEVELS = (4, 8, 16, 32)
GONE_Z = -29000  # BOS removals move references to z = -30000


def key_text(key):
    return f'{key[0]}:{key[1]:06X}'


def block(grid, level):
    return grid[0] // level * level, grid[1] // level * level


def removed(values):
    position = values.get('posA')
    return bool(values.get('flags', 0) & INITIALLY_DISABLED) or (position is not None and position[2] <= GONE_Z)


def main():
    config_path = Path(sys.argv[1] if len(sys.argv) > 1 else 'lodslicer.json').resolve()
    config = json.loads(config_path.read_text())
    data = (config_path.parent / config['data']).resolve()
    plugins = list(config['plugins'])
    order = load_order(data, plugins)
    statics = load_statics(data, order)
    loaded, unused = archives(data, plugin_order(data, config))
    print('archives, lowest priority first: ' + ', '.join(p.name for p in loaded))
    for path in unused:
        print(f'warning: ignoring {path.name}: no plugin in the load order loads it (add its plugin to "load_order")')
    available = lod_models(loaded)
    transforms, unsupported = swaps(data)
    swap_by_key = {key: (file, values) for file, key, values in transforms}

    refs = {name: [r for r in references(data, name) if not r['flags'] & DELETED] for name in plugins}
    report = defaultdict(Counter)
    chosen = {}
    for name, rules in config['plugins'].items():
        models = re.compile(rules.get('models', '.'), re.I)
        for r in refs[name]:
            swap = swap_by_key.get(r['key'])
            if (swap and removed(swap[1])) or r['position'][2] <= GONE_Z or r['flags'] & INITIALLY_DISABLED:
                report[name]['removed (disabled, or moved to z = -30000)'] += 1
                continue
            base = statics.get(r['base'])
            if not base:
                report[name]['base is not a STAT'] += 1
                continue
            if any(base['lod']):
                report[name]['base already has LOD models'] += 1
                continue
            stem = model_stem(base['model'])
            if stem not in available:
                report[name]['no LOD model exists'] += 1
                continue
            if not models.search(base['model']):
                report[name]['model not selected'] += 1
                continue
            if base['size'] * r['scale'] < rules.get('min_size', 0):
                report[name]['below size cut-off'] += 1
                continue
            report[name]['selected'] += 1
            chosen[r['base']] = base

    patch_refs = [r for name in plugins for r in refs[name] if r['base'] in chosen and r['key'] not in swap_by_key]
    swapped = [r for name in plugins for r in refs[name] if r['key'] in swap_by_key]
    missing_swaps = [key_text(k) for k in swap_by_key if k[0] in (p.lower() for p in plugins)
                     and k not in {r['key'] for r in swapped}]

    # Cells: everything the plugins place, plus where overridden vanilla references used to be.
    grids = {r['grid'] for name in plugins for r in refs[name] if r['world'][1] == 'Tamriel'}
    overridden = {r['key'] for name in plugins for r in refs[name] if not r['new']}
    for name in order:
        if name in plugins:
            continue
        for r in references(data, name, keys=overridden):
            if r['world'][1] == 'Tamriel':
                grids.add(r['grid'])
    grids = sorted(grids)
    blocks = {str(level): sorted({block(g, level) for g in grids}) for level in LEVELS}

    bases = {}
    for key, base in sorted(chosen.items(), key=lambda kv: kv[1]['editor_id']):
        bases[key_text(key)] = dict(
            editor_id=base['editor_id'], source=base['source'], model=base['model'], size=base['size'],
            lod=lod_plan(available[model_stem(base['model'])]))
    result = dict(
        config=config, order=order, bases=bases,
        patch_refs=[dict(source=r['source'], form_id=r['form_id'], key=key_text(r['key'])) for r in patch_refs],
        swaps=[dict(source=r['source'], form_id=r['form_id'], key=key_text(r['key']), file=swap_by_key[r['key']][0],
                    values=swap_by_key[r['key']][1]) for r in swapped],
        cells=grids, blocks=blocks)
    out = config_path.parent / 'targets.json'
    out.write_text(json.dumps(result, indent=1))

    print('load order:', ', '.join(order))
    for name in plugins:
        print(f'{name}: {len(refs[name])} references')
        for reason, count in report[name].most_common():
            print(f'  {count:5d} {reason}')
    print(f'\n{len(bases)} bases get LOD models, {len(patch_refs)} references in the plugins use them:')
    use = Counter(key_text(r['base']) for r in patch_refs)
    for key, b in bases.items():
        levels = [i for i, p in enumerate(b['lod']) if p]
        print(f'  {use[key]:4d} x {b["editor_id"]:<30} LOD4..32 slots {levels}  {b["lod"][0]}')
    print(f'\n{len(swapped)} references changed by swap files:')
    for r in swapped:
        file, values = swap_by_key[r['key']]
        base = statics.get(r['base'], {}).get('editor_id', key_text(r['base']))
        print(f'  {key_text(r["key"])} {base:<28} {values}  ({file})')
    for key in missing_swaps:
        print(f'  WARNING: swap target {key} not found in the plugins')
    for file, section, line in unsupported:
        print(f'  unsupported swap line ({file} [{section}]): {line}')
    xs, ys = [g[0] for g in grids], [g[1] for g in grids]
    print(f'\n{len(grids)} cells, x {min(xs)}..{max(xs)}, y {min(ys)}..{max(ys)}')
    print('blocks:', ', '.join(f'LOD{level} {len(b)}' for level, b in blocks.items()))
    print(f'wrote {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

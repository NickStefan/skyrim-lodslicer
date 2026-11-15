# lodslicer

Object LOD for a few mods' cells in Skyrim Special Edition, without regenerating anyone else's.

Mods that add things to the landscape (rocks, islands, villages) often leave them out of the distant view, so
they pop in as you approach. The usual fix is to regenerate object LOD for the whole world with xLODGen or
DynDOLOD, which replaces every block of object LOD you have. lodslicer lets xLODGen do the mesh work, then
keeps only the blocks around the mods you care about and packages them as a small, self-contained mod that
sits on top of your existing LOD.

It is a handful of Python scripts with no dependencies beyond the standard library.

## How it works

1. **`analyze.py`** reads the mods' plugins and decides which placed objects should get LOD and which LOD
   blocks cover them. It writes `targets.json`.
2. **`make_patch.py`** writes a throwaway "LOD generation patch" plugin. It gives the chosen base objects LOD
   models and flags their references *Visible When Distant*, so xLODGen includes them. It also applies the
   mods' Base Object Swapper (`*_SWAP.ini`) removals and moves, which xLODGen does not read.
3. **You run xLODGen** against your real load order with the patch loaded last, then remove the patch.
4. **`slice.py`** keeps only the target blocks of xLODGen's output, renames xLODGen's atlas so it cannot
   clash with vanilla's or another generation's, fixes textures object LOD can't display, checks the result
   and packs it into a BSA plus an empty light plugin that loads it.

Nothing here touches your game installation. You copy the patch into the game and the output back by hand.

## Requirements

- Python 3.10 or newer.
- xLODGen (SSE). It runs fine under Proton on Linux and the Steam Deck; see [Running xLODGen on Linux](#running-xlodgen-on-linux).
- Copies of the game files listed below, in a data folder lodslicer can read.

## Setting up a job

Copy the example config and edit it:

```sh
cp lodslicer.example.json lodslicer.json
```

Put copies of these in the folder named by `"data"` (default `data/`):

- **Masters:** `Skyrim.esm`, `Update.esm`, `Dawnguard.esm`, `HearthFires.esm`, `Dragonborn.esm`.
- **The mods' plugins,** their own archives, and their `*_SWAP.ini` files if they have any.
- **Vanilla mesh archives:** `Skyrim - Meshes0.bsa` and `Skyrim - Meshes1.bsa`. They're used to find LOD
  models and to compare the new blocks with vanilla's.
- **LOD model archives** with `<model>_lod_<level>.nif` files, for example `DynDOLOD Resources.bsa`.
- **Texture archives** for any texture listed in `opaque_textures`: the vanilla `Skyrim - Textures*.bsa`, or
  the archive of a texture mod you use.

`data/` is empty in the repository and ignored by git. Game files can't be shared, so copy in your own.

All scripts take an optional config path and default to `lodslicer.json`. Paths in the config are relative
to the config file.

### Archive order

When several archives contain the same file, lodslicer uses the one the game would. The vanilla
`Skyrim - *.bsa` archives load first. After them come each plugin's `<plugin>.bsa` and
`<plugin> - Textures.bsa`, in load order, and later ones override earlier ones. An archive with no
matching plugin isn't loaded by the game, so `analyze.py` warns about it and lodslicer ignores it.

List the plugins that own archives in `load_order`, in the order they appear in your real load order. The
plugin files themselves don't need to be in `data/`; for example, `"DynDOLOD Resources.esp"` makes
`DynDOLOD Resources.bsa` count. The job's own plugins go after the listed ones unless you list them yourself,
and their masters go before. `analyze.py` prints the resulting archive order.

### Config reference

| Key | Meaning |
|---|---|
| `data` | Folder holding the copies above. |
| `plugins` | The mods to generate LOD for, each with selection rules (below). |
| `load_order` | Optional. Plugins from your load order that own archives in `data`, in load order (see [Archive order](#archive-order)). |
| `patch` | File name of the LOD generation patch, written to `patch/`. |
| `xlodgen_output` | Folder you copy xLODGen's output into. |
| `xlodgen_atlas` | Name of the atlas xLODGen wrote, without `.dds`. |
| `atlas` | New atlas name; must be the same length as `xlodgen_atlas`. |
| `package` | Name of the BSA and plugin written to `result/`. |
| `opaque_textures` | Optional. Textures whose alpha channel must be dropped for object LOD (see below). |
| `opaque_max_size` | Optional. Largest size, in pixels, for those copies; larger mip levels are dropped. |

Each plugin's rules choose which of its references get LOD:

- **`models`** is a regular expression matched against the base object's model path. For example,
  `"\\\\rocks\\\\"` selects models under a `rocks` folder, and `"."` matches everything.
- **`min_size`** is the smallest size, in game units after scaling, a reference must have to qualify.

A reference qualifies when its base is a static with no LOD of its own, a LOD model exists for that model, and
it passes both rules. LOD belongs to the base object, so one qualifying reference gives every reference of
that base LOD. References whose base already has LOD are kept as they are.

## Running a job

```sh
python3 analyze.py        # writes targets.json and prints what it chose
python3 make_patch.py     # writes patch/<patch name>
```

Then, in your game installation:

1. Copy the patch into your Data folder (or install it as a mod), enable it and load it **last**.
2. Run xLODGen with the settings below and generate.
3. **Disable and remove the patch.** It strips data from references and must never be played with.
4. Copy the output folder's contents into `xlodgen-output/`, so that `xlodgen-output/meshes/terrain/...` and
   `xlodgen-output/textures/terrain/...` exist.

Then:

```sh
python3 slice.py          # writes result/<package>.bsa and result/<package>.esp
```

Install the two files in `result/` as a mod, loaded after anything else that ships object LOD for the same
blocks.

### xLODGen settings

- **Worldspace:** Tamriel only.
- **LOD types:** Objects only. Trees, terrain, occlusion, seasons and "trees in object LOD" all off.
  Your existing tree and terrain LOD use different files (`.btt`, `.btr`), so they're left alone.
- **Atlas:** on, 4096 × 4096. Go up to 8192 × 4096 if the log says the atlas overflowed.
- **Max texture size:** 512. Larger textures are referenced directly instead of being put on the atlas.
- **UV range, alpha threshold, backlight, vertex colors:** defaults.
- **Compression:** DXT1 for diffuse, DXT1 or 565 for normals.

## What slice.py checks

- **Every selected reference has geometry where it stands** in its LOD4 block. References that sit entirely
  under water are counted separately: xLODGen drops object LOD triangles below the water line, as it should.
- **Objects the swap files remove don't reappear.** Geometry found near a removed reference is reported as a
  note; usually it belongs to something else standing there now.
- **Coverage against vanilla:** for each block, the areas above water where vanilla had geometry and the new
  block has none. Small losses next to objects that are still present usually mean your load order uses a
  different LOD model for them.
- **The archive reads back** byte for byte.

## Textures with alpha: `opaque_textures`

Object LOD always alpha tests at a fixed threshold: pixels with alpha above 128 show, anything else is
invisible. Some textures carry an alpha channel the full model ignores, so the object looks solid up close but
has holes, or disappears entirely, in LOD. DynDOLOD works around this with its own LOD textures; xLODGen
references the original texture directly when it doesn't fit on the atlas.

The usual sign is an object where only some parts show in LOD, such as ships showing their bow but not their
hull. List the offending texture in `opaque_textures`. `slice.py` then writes a copy without alpha to
`textures\lod\<atlas>\` (the colours are unchanged) and points the blocks at it. The copy comes from
whichever archive the game would load the texture from. So if you use a texture mod, copy its archive into
`data/` and add its plugin to `load_order`, and the LOD will match what you see up close.

`python3 -m lodslicer.bsa list ARCHIVE PATTERN` lists the files in an archive, which helps find the texture.

## Limitations

- **Tamriel only.** Block paths and the water height are Tamriel's.
- **Statics only.** Only `STAT` bases are given LOD.
- **Base Object Swapper:** only `[Transforms]` lines with `posA` and `flags` are reproduced. Other sections
  and base swaps are reported by `analyze.py` and otherwise ignored.
- **Archives only.** Loose files in `data/` aren't read; LOD models and textures must be in archives.
- **The atlas** is xLODGen's whole-world atlas. Only one copy is shipped, but it's about 11 MB for each of the
  diffuse and normal maps at 4096².

## Running xLODGen on Linux

xLODGen has to run inside the game's Proton prefix so it finds the load order. With the protontricks
Flatpak:

```sh
cd /path/to/xLODGen && \
flatpak run --command=protontricks-launch com.github.Matoking.protontricks \
  --appid <game app id> "$PWD/xLODGenx64.exe" -sse \
  -o:"Z:/path/to/output" \
  -d:"Z:/path/to/Skyrim/Data/" \
  -m:"Z:/path/to/compatdata/<game app id>/pfx/drive_c/users/steamuser/Documents/My Games/Skyrim Special Edition/"
```

Or, with Proton directly (use the Proton version the prefix was last run with):

```sh
STEAM_COMPAT_CLIENT_INSTALL_PATH=~/.local/share/Steam \
STEAM_COMPAT_DATA_PATH=~/.local/share/Steam/steamapps/compatdata/<game app id> \
~/.local/share/Steam/compatibilitytools.d/<proton>/proton run xLODGenx64.exe -sse -o:"..." -d:"..." -m:"..."
```

## Layout

```
analyze.py, make_patch.py, slice.py   the three steps
lodslicer/
  records.py   load order, statics, references, swap files, LOD models
  esm.py       reading and writing plugins
  bsa.py       reading and writing archives (v105, SE)
  nif.py       object LOD shapes in world space; texture path rewriting
  dds.py       dropping a texture's alpha channel
lodslicer.example.json
```

## License

GPL-3.0; see [LICENSE](LICENSE). The plugin and archive code started out in
[Skiing in Skyrim](https://github.com/NickStefan/SkiingInSkyrim), under the same license.

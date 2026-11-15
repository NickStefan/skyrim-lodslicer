"""Just enough of Skyrim SE NIF files to place object LOD geometry in the world and swap its textures."""
import struct

SHAPES = ("BSTriShape", "BSDynamicTriShape", "BSMeshLODTriShape", "BSLODTriShape", "BSSubIndexTriShape")


def _layout(data):
    """(bs_version, block types, block sizes, offset of the block size table, offset of the first block)."""
    i = data.index(b"\n") + 1
    _, _, _, blocks = struct.unpack_from("<IBII", data, i)
    i += 13
    bs_version = struct.unpack_from("<I", data, i)[0]
    i += 4
    for _ in range(3):  # author, process script, export script
        i += 1 + data[i]
    type_count = struct.unpack_from("<H", data, i)[0]
    i += 2
    types = []
    for _ in range(type_count):
        length = struct.unpack_from("<I", data, i)[0]
        types.append(data[i + 4:i + 4 + length].decode())
        i += 4 + length
    block_types = struct.unpack_from(f"<{blocks}H", data, i)
    i += 2 * blocks
    sizes_at = i
    sizes = struct.unpack_from(f"<{blocks}I", data, i)
    i += 4 * blocks
    string_count, _ = struct.unpack_from("<II", data, i)
    i += 8
    for _ in range(string_count):
        i += 4 + struct.unpack_from("<I", data, i)[0]
    i += 4 + 4 * struct.unpack_from("<I", data, i)[0]  # groups
    return bs_version, [types[t & 0x7FFF] for t in block_types], list(sizes), sizes_at, i


def shapes(data):
    """Yields (block type, world positions of its vertices) for each tri shape, applying the shape's own
    translation, rotation and scale (object LOD stores vertices relative to the block corner, scaled down by
    the LOD level)."""
    bs_version, kinds, sizes, _, i = _layout(data)
    for kind, size in zip(kinds, sizes):
        start = i
        i += size
        if kind not in SHAPES:
            continue
        j = start + 4  # name
        j += 4 + 4 * struct.unpack_from("<I", data, j)[0] + 4  # extra data, controller
        j += 4  # flags
        translation = struct.unpack_from("<3f", data, j)
        rotation = struct.unpack_from("<9f", data, j + 12)
        scale = struct.unpack_from("<f", data, j + 48)[0]
        j += 12 + 36 + 4 + 4  # translation, rotation, scale, collision
        j += 16 + 12  # bounding sphere; skin, shader, alpha
        desc = struct.unpack_from("<Q", data, j)[0]
        j += 8
        vertices = struct.unpack_from("<H", data, j + 2)[0]
        j += 4 + 4  # counts, data size
        stride = (desc & 0xF) * 4
        full = bool((desc >> 44) & 0x400) or bs_version == 100
        fmt = "<3f" if full else "<3e"
        points = []
        for v in range(vertices):
            x, y, z = struct.unpack_from(fmt, data, j + v * stride)
            r = rotation
            points.append((translation[0] + scale * (r[0] * x + r[1] * y + r[2] * z),
                           translation[1] + scale * (r[3] * x + r[4] * y + r[5] * z),
                           translation[2] + scale * (r[6] * x + r[7] * y + r[8] * z)))
        yield kind, points


def retexture(data, replace):
    """Rewrites the paths in BSShaderTextureSet blocks with replace(path), which returns the path unchanged
    or a new one. Returns (new data, number of paths changed)."""
    _, kinds, sizes, sizes_at, i = _layout(data)
    out, changed = [data[:i]], 0
    for index, (kind, size) in enumerate(zip(kinds, sizes)):
        block = data[i:i + size]
        i += size
        if kind == "BSShaderTextureSet":
            count = struct.unpack_from("<i", block, 0)[0]
            j, paths = 4, []
            for _ in range(count):
                length = struct.unpack_from("<I", block, j)[0]
                paths.append(block[j + 4:j + 4 + length].decode())
                j += 4 + length
            new = [replace(p) for p in paths]
            if new != paths:
                changed += sum(a != b for a, b in zip(paths, new))
                block = struct.pack("<i", count) + b"".join(struct.pack("<I", len(p)) + p.encode() for p in new)
                sizes[index] = len(block)
        out.append(block)
    out.append(data[i:])
    result = bytearray(b"".join(out))
    struct.pack_into(f"<{len(sizes)}I", result, sizes_at, *sizes)
    return bytes(result), changed

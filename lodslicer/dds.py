"""Block-compressed DDS textures: just enough to drop an alpha channel object LOD would alpha test against."""
import struct

HEADER = 128
DDSD_MIPMAPCOUNT = 0x20000
BLOCK_SIZES = {b'DXT1': 8, b'DXT3': 16, b'DXT5': 16}


def opaque(data, max_size=None):
    """A DXT1 copy of a DXT1, DXT3 or DXT5 texture without any transparency, starting at the first mip level
    no larger than max_size. Colour blocks are reused as they are, only reordered where DXT1 would read them
    as having a transparent colour, so no pixel changes."""
    fourcc = data[84:88]
    if fourcc not in BLOCK_SIZES:
        raise ValueError(f'unsupported texture format {fourcc!r}')
    stride = BLOCK_SIZES[fourcc]
    height, width = struct.unpack_from('<II', data, 12)
    flags, mips = struct.unpack_from('<I', data, 8)[0], struct.unpack_from('<I', data, 28)[0]
    levels = mips if flags & DDSD_MIPMAPCOUNT and mips else 1
    skip = 0
    while max_size and skip < levels - 1 and max(width, height) >> skip > max_size:
        skip += 1
    blocks = []
    at = HEADER
    for level in range(levels):
        count = max(1, (width >> level) // 4) * max(1, (height >> level) // 4)
        if level >= skip:
            for b in range(count):
                c0, c1, indices = struct.unpack_from('<HHI', data, at + b * stride + stride - 8)
                if c0 < c1:
                    c0, c1 = c1, c0
                    indices ^= 0x55555555  # 0<->1, 2<->3
                elif c0 == c1:
                    indices = 0
                blocks.append(struct.pack('<HHI', c0, c1, indices))
        at += count * stride
    width, height = max(1, width >> skip), max(1, height >> skip)
    header = bytearray(data[:HEADER])
    header[84:88] = b'DXT1'
    struct.pack_into('<II', header, 12, height, width)
    struct.pack_into('<I', header, 20, max(1, width // 4) * max(1, height // 4) * 8)  # linear size
    if flags & DDSD_MIPMAPCOUNT:
        struct.pack_into('<I', header, 28, levels - skip)
    return bytes(header) + b''.join(blocks)

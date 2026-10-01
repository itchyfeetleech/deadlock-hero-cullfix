"""Minimal Source 2 compiled-resource toolkit.

Reads and writes the resource container, binary KV3 (reads v1-v5 layouts used
by current Deadlock, writes uncompressed v5 like ValveResourceFormat's
serializer) and RERL external reference lists. Scalar KV3 types and flags are
preserved on round trip so untouched data is re-emitted with identical
semantics.
"""
from __future__ import annotations

import struct
import uuid

# ---------------------------------------------------------------- typed values

class I16(int): pass
class U16(int): pass
class I32(int): pass
class U32(int): pass
class U64(int): pass
class F32(float): pass


class FStr(str):
    """String carrying a KV3 flag (resource:, resource_name:, soundevent:, ...)."""
    def __new__(cls, value, flag=0):
        obj = str.__new__(cls, value)
        obj.flag = flag
        return obj

    def __reduce__(self):
        return (FStr, (str(self), self.flag))


class KVObj(dict):
    flag = 0


class KVArr(list):
    flag = 0


FLAG_RESOURCE = 1
FLAG_RESOURCE_NAME = 2
FLAG_PANORAMA = 3
FLAG_SOUNDEVENT = 4
FLAG_SUBCLASS = 5

(T_NULL, T_BOOL, T_INT64, T_UINT64, T_DOUBLE, T_STRING, T_BLOB, T_ARRAY, T_OBJECT,
 T_ARRAY_TYPED, T_INT32, T_UINT32, T_TRUE, T_FALSE, T_INT64_ZERO, T_INT64_ONE,
 T_DOUBLE_ZERO, T_DOUBLE_ONE, T_FLOAT, T_INT16, T_UINT16, T_UNK22, T_INT32_AS_BYTE,
 T_ARRAY_BYTE_LEN, T_ARRAY_AUX) = range(1, 26)

KV3_TRAILER = 0xFFEEDD00


def align(n, a):
    return (n + a - 1) // a * a

# ---------------------------------------------------------------- KV3 reading

class _Lane:
    __slots__ = ('b1', 'b2', 'b4', 'b8', 'p1', 'p2', 'p4', 'p8')

    def __init__(self, b1=b'', b2=b'', b4=b'', b8=b''):
        self.b1, self.b2, self.b4, self.b8 = b1, b2, b4, b8
        self.p1 = self.p2 = self.p4 = self.p8 = 0

    def u8(self):
        v = self.b1[self.p1]; self.p1 += 1; return v

    def take(self, fmt, size, lane):
        buf = getattr(self, 'b' + lane); pos = getattr(self, 'p' + lane)
        v = struct.unpack_from(fmt, buf, pos)[0]
        setattr(self, 'p' + lane, pos + size)
        return v


class _ReadCtx:
    pass


def _read_type(ctx):
    t = ctx.types[ctx.tp]; ctx.tp += 1
    flag = 0
    if ctx.version >= 3:
        if t & 0x80:
            t &= 0x3F
            flag = ctx.types[ctx.tp]; ctx.tp += 1
    elif t & 0x80:
        t &= 0x7F
        f = ctx.types[ctx.tp]; ctx.tp += 1
        f &= ~4
        flag = {0: 0, 1: FLAG_RESOURCE, 2: FLAG_RESOURCE_NAME, 8: FLAG_PANORAMA,
                16: FLAG_SOUNDEVENT, 32: FLAG_SUBCLASS}[f]
    return t, flag


def _apply_flag(v, flag):
    if not flag:
        return v
    if isinstance(v, str):
        return FStr(v, flag)
    if isinstance(v, (KVObj, KVArr)):
        v.flag = flag
        return v
    raise ValueError(f'flag {flag} on unsupported value {type(v)}')


def _read_value(ctx, t, flag):
    L = ctx.buf
    if t == T_NULL: v = None
    elif t == T_TRUE: v = True
    elif t == T_FALSE: v = False
    elif t == T_INT64_ZERO: v = 0
    elif t == T_INT64_ONE: v = 1
    elif t == T_DOUBLE_ZERO: v = 0.0
    elif t == T_DOUBLE_ONE: v = 1.0
    elif t == T_BOOL: v = L.u8() == 1
    elif t == T_INT32_AS_BYTE: v = I32(L.u8())
    elif t == T_INT16: v = I16(L.take('<h', 2, '2'))
    elif t == T_UINT16: v = U16(L.take('<H', 2, '2'))
    elif t == T_INT32: v = I32(L.take('<i', 4, '4'))
    elif t == T_UINT32: v = U32(L.take('<I', 4, '4'))
    elif t == T_FLOAT: v = F32(L.take('<f', 4, '4'))
    elif t == T_INT64: v = L.take('<q', 8, '8')
    elif t == T_UINT64: v = U64(L.take('<Q', 8, '8'))
    elif t == T_DOUBLE: v = L.take('<d', 8, '8')
    elif t == T_STRING:
        i = L.take('<i', 4, '4'); v = '' if i == -1 else ctx.strings[i]
    elif t == T_BLOB:
        if ctx.version < 2:
            n = L.take('<i', 4, '4'); v = bytes(L.b1[L.p1:L.p1 + n]); L.p1 += n
        else:
            n = struct.unpack_from('<i', ctx.blob_lengths, ctx.blp)[0]; ctx.blp += 4
            v = bytes(ctx.blobs[ctx.bp:ctx.bp + n]); ctx.bp += n
    elif t == T_ARRAY:
        n = L.take('<i', 4, '4'); v = KVArr()
        for _ in range(n):
            st, sf = _read_type(ctx); v.append(_read_value(ctx, st, sf))
    elif t in (T_ARRAY_TYPED, T_ARRAY_BYTE_LEN):
        n = L.u8() if t == T_ARRAY_BYTE_LEN else L.take('<i', 4, '4')
        st, sf = _read_type(ctx); v = KVArr()
        for _ in range(n):
            v.append(_read_value(ctx, st, sf))
    elif t == T_ARRAY_AUX:
        n = L.u8(); st, sf = _read_type(ctx); v = KVArr()
        ctx.buf, ctx.aux = ctx.aux, ctx.buf
        for _ in range(n):
            v.append(_read_value(ctx, st, sf))
        ctx.buf, ctx.aux = ctx.aux, ctx.buf
    elif t == T_OBJECT:
        if ctx.version >= 5:
            n = struct.unpack_from('<i', ctx.objlens, ctx.op)[0]; ctx.op += 4
        else:
            n = L.take('<i', 4, '4')
        v = KVObj()
        for _ in range(n):
            st, sf = _read_type(ctx)
            ki = ctx.buf.take('<i', 4, '4')
            key = '' if ki == -1 else ctx.strings[ki]
            v[key] = _read_value(ctx, st, sf)
    else:
        raise ValueError(f'unknown KV3 type {t}')
    return _apply_flag(v, flag)


def _decompress(method, data, usize):
    if method == 0:
        return data
    if method == 1:
        import lz4.block
        return lz4.block.decompress(data, uncompressed_size=usize)
    if method == 2:
        import zstandard
        return zstandard.ZstdDecompressor().decompress(data, max_output_size=usize)
    raise ValueError(method)


def kv3_read(data: bytes):
    """Return (format_guid_bytes, root) for a binary KV3 v4/v5 block."""
    magic = struct.unpack_from('<I', data, 0)[0]
    version = magic & 0xFF
    if magic & 0xFFFFFF00 != 0x4B563300 or version not in (4, 5):
        raise ValueError(f'unsupported KV3 magic {magic:#x}')
    fmt = data[4:20]
    (method, dict_id, frame, c1, c4, c8, ctypes, cobj, carr, usize, csize,
     cblocks, blobbytes, c2, sizes_bytes) = struct.unpack_from('<IHHiiiiHHiiiiii', data, 20)
    pos = 20 + struct.calcsize('<IHHiiiiHHiiiiii')
    ctx = _ReadCtx(); ctx.version = version
    if version >= 5:
        (u1, cc1, u2, cc2, b1_2, b2_2, b4_2, b8_2, _unk, cobj2, carr2, _unk2) = struct.unpack_from('<12i', data, pos)
        pos += 48
        raw1 = data[pos:pos + (cc1 if method else u1)]; pos += cc1 if method else u1
        buf1 = _decompress(method, raw1, u1)
        raw2 = data[pos:pos + (cc2 if method else u2)]; pos += cc2 if method else u2
        buf2 = _decompress(method, raw2, u2)
        # buffer 1: strings + aux lanes
        o = 0
        b1 = buf1[o:o + c1]; o += c1
        b2 = b''
        if c2: o = align(o, 2); b2 = buf1[o:o + c2 * 2]; o += c2 * 2
        o = align(o, 4); b4 = buf1[o:o + c4 * 4]; o += c4 * 4
        b8 = b''
        if c8: o = align(o, 8); b8 = buf1[o:o + c8 * 8]; o += c8 * 8
        nstr = struct.unpack_from('<i', b4, 0)[0]
        strs = b1.split(b'\0')
        ctx.strings = [s.decode('utf-8') for s in strs[:nstr]]
        str_bytes = sum(len(s) + 1 for s in strs[:nstr])
        aux = _Lane(b1[str_bytes:], b2, b4[4:], b8)
        ctx.aux = aux
        # buffer 2: object lengths, lanes, types
        o = cobj2 * 4
        ctx.objlens = buf2[:o]; ctx.op = 0
        lb1 = buf2[o:o + b1_2]; o += b1_2
        lb2 = b''
        if b2_2: o = align(o, 2); lb2 = buf2[o:o + b2_2 * 2]; o += b2_2 * 2
        lb4 = b''
        if b4_2: o = align(o, 4); lb4 = buf2[o:o + b4_2 * 4]; o += b4_2 * 4
        lb8 = b''
        if b8_2: o = align(o, 8); lb8 = buf2[o:o + b8_2 * 8]; o += b8_2 * 8
        ctx.buf = _Lane(lb1, lb2, lb4, lb8)
        ctx.types = buf2[o:o + ctypes]; o += ctypes; ctx.tp = 0
        if cblocks:
            ctx.blob_lengths = buf2[o:o + cblocks * 4]; o += cblocks * 4
            assert struct.unpack_from('<I', buf2, o)[0] == KV3_TRAILER
            if method == 0:
                ctx.blobs = data[pos:pos + blobbytes]; pos += blobbytes
            elif method == 2:
                comp = csize - cc1 - cc2
                ctx.blobs = _decompress(2, data[pos:pos + comp], blobbytes); pos += comp
            else:
                import lz4.block
                sizes = struct.unpack_from(f'<{sizes_bytes // 2}H', buf2, o + 4)
                out = bytearray()
                for s in sizes:
                    n = min(frame, blobbytes - len(out))
                    out += lz4.block.decompress(data[pos:pos + s], uncompressed_size=n,
                                                dict=bytes(out[-65536:]))
                    pos += s
                assert len(out) == blobbytes
                ctx.blobs = bytes(out)
            ctx.blp = ctx.bp = 0
        else:
            assert struct.unpack_from('<I', buf2, o)[0] == KV3_TRAILER
    else:
        # v4: one buffer holding lanes, then strings, types, blob lengths
        raw = data[pos:pos + (csize if method else usize)]; pos += csize if method else usize
        if method == 2:
            buf = _decompress(2, raw, usize + blobbytes)
        else:
            buf = _decompress(method, raw, usize)
        o = 0
        b1 = buf[o:o + c1]; o += c1
        b2 = b''
        if c2: o = align(o, 2); b2 = buf[o:o + c2 * 2]; o += c2 * 2
        o = align(o, 4); b4 = buf[o:o + c4 * 4]; o += c4 * 4
        o = align(o, 8)
        b8 = buf[o:o + c8 * 8]; o += c8 * 8
        nstr = struct.unpack_from('<i', b4, 0)[0]
        start = o
        strs = []
        for _ in range(nstr):
            e = buf.index(b'\0', o); strs.append(buf[o:e].decode('utf-8')); o = e + 1
        ctx.strings = strs
        tlen = ctypes - (o - start)
        ctx.types = buf[o:o + tlen]; o += tlen; ctx.tp = 0
        ctx.buf = _Lane(b1, b2, b4[4:], b8)
        ctx.aux = None
        if cblocks:
            ctx.blob_lengths = buf[o:o + cblocks * 4]; o += cblocks * 4
            assert struct.unpack_from('<I', buf, o)[0] == KV3_TRAILER; o += 4
            if method == 0:
                ctx.blobs = data[pos:pos + blobbytes]
            elif method == 2:
                ctx.blobs = buf[usize:usize + blobbytes]
            else:
                import lz4.block
                sizes = struct.unpack_from(f'<{(len(buf) - o) // 2}H', buf, o)
                out = bytearray()
                for s in sizes:
                    n = min(frame, blobbytes - len(out))
                    out += lz4.block.decompress(data[pos:pos + s], uncompressed_size=n, dict=bytes(out[-65536:]))
                    pos += s
                ctx.blobs = bytes(out)
            ctx.blp = ctx.bp = 0
        else:
            assert struct.unpack_from('<I', buf, o)[0] == KV3_TRAILER
    t, f = _read_type(ctx)
    root = _read_value(ctx, t, f)
    assert ctx.tp == len(ctx.types), (ctx.tp, len(ctx.types))
    return fmt, root

# ---------------------------------------------------------------- KV3 writing

class _WriteCtx:
    def __init__(self):
        self.strings = {}
        self.b1 = bytearray(); self.b2 = bytearray(); self.b4 = bytearray(); self.b8 = bytearray()
        self.types = bytearray(); self.objlens = bytearray(); self.blobs = bytearray()
        self.bloblens = []; self.arrays = 0; self.nodes = 0

    def sid(self, s):
        if not s:
            return -1
        if s not in self.strings:
            self.strings[s] = len(self.strings)
        return self.strings[s]


def _wtype(ctx, t, flag):
    if flag:
        ctx.types += bytes((t | 0x80, flag))
    else:
        ctx.types.append(t)


def _write_value(ctx, v):
    ctx.nodes += 1
    flag = getattr(v, 'flag', 0)
    if v is None: _wtype(ctx, T_NULL, flag)
    elif v is True: _wtype(ctx, T_TRUE, flag)
    elif v is False: _wtype(ctx, T_FALSE, flag)
    elif isinstance(v, I16): _wtype(ctx, T_INT16, flag); ctx.b2 += struct.pack('<h', v)
    elif isinstance(v, U16): _wtype(ctx, T_UINT16, flag); ctx.b2 += struct.pack('<H', v)
    elif isinstance(v, I32): _wtype(ctx, T_INT32, flag); ctx.b4 += struct.pack('<i', v)
    elif isinstance(v, U32): _wtype(ctx, T_UINT32, flag); ctx.b4 += struct.pack('<I', v)
    elif isinstance(v, U64): _wtype(ctx, T_UINT64, flag); ctx.b8 += struct.pack('<Q', v)
    elif isinstance(v, int):
        if v == 0: _wtype(ctx, T_INT64_ZERO, flag)
        elif v == 1: _wtype(ctx, T_INT64_ONE, flag)
        else: _wtype(ctx, T_INT64, flag); ctx.b8 += struct.pack('<q', v)
    elif isinstance(v, F32): _wtype(ctx, T_FLOAT, flag); ctx.b4 += struct.pack('<f', v)
    elif isinstance(v, float):
        if struct.pack('<d', v) == b'\0' * 8: _wtype(ctx, T_DOUBLE_ZERO, flag)
        elif v == 1.0: _wtype(ctx, T_DOUBLE_ONE, flag)
        else: _wtype(ctx, T_DOUBLE, flag); ctx.b8 += struct.pack('<d', v)
    elif isinstance(v, str): _wtype(ctx, T_STRING, flag); ctx.b4 += struct.pack('<i', ctx.sid(str(v)))
    elif isinstance(v, (bytes, bytearray)):
        _wtype(ctx, T_BLOB, flag); ctx.bloblens.append(len(v)); ctx.blobs += v
    elif isinstance(v, dict):
        _wtype(ctx, T_OBJECT, flag); ctx.objlens += struct.pack('<i', len(v))
        for k, item in v.items():
            ctx.b4 += struct.pack('<i', ctx.sid(k))
            _write_value(ctx, item)
    elif isinstance(v, (list, tuple)):
        _wtype(ctx, T_ARRAY, flag); ctx.arrays += 1; ctx.b4 += struct.pack('<i', len(v))
        for item in v:
            _write_value(ctx, item)
    else:
        raise TypeError(type(v))


def _lane(out, lane, alignment):
    if lane:
        out += b'\0' * (align(len(out), alignment) - len(out))
        out += lane


def kv3_write(root, fmt: bytes) -> bytes:
    """Serialize as uncompressed binary KV3 v5 (layout matches VRF's writer)."""
    ctx = _WriteCtx()
    _write_value(ctx, root)
    buf1 = bytearray()
    for s in ctx.strings:
        buf1 += s.encode('utf-8') + b'\0'
    strbytes = len(buf1)
    buf1 += b'\0' * (align(len(buf1), 4) - len(buf1))
    buf1 += struct.pack('<i', len(ctx.strings))
    buf2 = bytearray(ctx.objlens)
    _lane(buf2, ctx.b1, 1); _lane(buf2, ctx.b2, 2); _lane(buf2, ctx.b4, 4); _lane(buf2, ctx.b8, 8)
    buf2 += ctx.types
    for n in ctx.bloblens:
        buf2 += struct.pack('<i', n)
    buf2 += struct.pack('<I', KV3_TRAILER)
    nobj = len(ctx.objlens) // 4
    out = bytearray(struct.pack('<I', 0x4B563305)) + fmt
    out += struct.pack('<IHH', 0, 0, 0)
    out += struct.pack('<iiiiHHiiiiii', strbytes, 1, 0, len(ctx.types), nobj & 0xFFFF, ctx.arrays & 0xFFFF,
                       len(buf1) + len(buf2), len(buf1) + len(buf2) + len(ctx.blobs), len(ctx.bloblens),
                       len(ctx.blobs), 0, 0)
    out += struct.pack('<12i', len(buf1), 0, len(buf2), 0, len(ctx.b1), len(ctx.b2) // 2, len(ctx.b4) // 4,
                       len(ctx.b8) // 8, ctx.nodes, nobj, ctx.arrays, 0)
    out += buf1 + buf2 + ctx.blobs
    if ctx.bloblens:
        out += struct.pack('<I', KV3_TRAILER)
    return bytes(out)

# ---------------------------------------------------------------- KV3 text (debug)

def kv3_text(v, indent=0):
    pad = '\t' * indent
    prefix = {FLAG_RESOURCE: 'resource:', FLAG_RESOURCE_NAME: 'resource_name:', FLAG_PANORAMA: 'panorama:',
              FLAG_SOUNDEVENT: 'soundevent:', FLAG_SUBCLASS: 'subclass:'}.get(getattr(v, 'flag', 0), '')
    if isinstance(v, dict):
        body = ''.join(f'{pad}\t{k} = {kv3_text(x, indent + 1)}\n' for k, x in v.items())
        return prefix + '{\n' + body + pad + '}'
    if isinstance(v, list):
        return prefix + '[\n' + ''.join(f'{pad}\t{kv3_text(x, indent + 1)},\n' for x in v) + pad + ']'
    if isinstance(v, str):
        return prefix + '"' + v.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if v is None:
        return 'null'
    if isinstance(v, (bytes, bytearray)):
        return f'#[{len(v)} bytes]'
    return repr(v) if not isinstance(v, float) else f'{v:.6g}'

# ---------------------------------------------------------------- resource container

class Resource:
    def __init__(self, version=1, blocks=None):
        self.version = version
        self.blocks = blocks or []   # list of [fourcc, bytes]

    @classmethod
    def parse(cls, data: bytes):
        size, hv, ver, boff, count = struct.unpack_from('<IHHII', data, 0)
        assert hv == 12, hv
        base = 8 + boff
        blocks = []
        for i in range(count):
            o = base + i * 12
            t = data[o:o + 4].decode('ascii')
            off, sz = struct.unpack_from('<II', data, o + 4)
            start = o + 4 + off
            blocks.append([t, bytes(data[start:start + sz])])
        return cls(ver, blocks)

    def get(self, fourcc, index=0):
        return [b for t, b in self.blocks if t == fourcc][index]

    def build(self) -> bytes:
        n = len(self.blocks)
        out = bytearray(struct.pack('<IHHII', 0, 12, self.version, 8, n))
        table = len(out)
        out += b'\0' * (12 * n)
        for i, (t, data) in enumerate(self.blocks):
            out += b'\0' * (align(len(out), 16) - len(out))
            start = len(out)
            out += data
            entry = table + 12 * i
            struct.pack_into('<4sII', out, entry, t.encode('ascii'), start - (entry + 4), len(data))
        struct.pack_into('<I', out, 0, len(out))
        return bytes(out)

# ---------------------------------------------------------------- RERL

def murmur64b(data: bytes, seed: int = 0xEDABCDEF) -> int:
    m, r, M32 = 0x5bd1e995, 24, 0xFFFFFFFF
    n = len(data)
    h1 = (seed ^ n) & M32; h2 = (seed >> 32) & M32; i = 0

    def mix(k):
        k = (k * m) & M32; k ^= k >> r; return (k * m) & M32
    while n >= 8:
        h1 = ((h1 * m) & M32) ^ mix(int.from_bytes(data[i:i + 4], 'little')); i += 4
        h2 = ((h2 * m) & M32) ^ mix(int.from_bytes(data[i:i + 4], 'little')); i += 4
        n -= 8
    if n >= 4:
        h1 = ((h1 * m) & M32) ^ mix(int.from_bytes(data[i:i + 4], 'little')); i += 4; n -= 4
    if n == 3: h2 ^= data[i + 2] << 16
    if n >= 2: h2 ^= data[i + 1] << 8
    if n >= 1: h2 ^= data[i]; h2 = (h2 * m) & M32
    h1 ^= h2 >> 18; h1 = (h1 * m) & M32; h2 ^= h1 >> 22; h2 = (h2 * m) & M32
    h1 ^= h2 >> 17; h1 = (h1 * m) & M32; h2 ^= h1 >> 19; h2 = (h2 * m) & M32
    return (h1 << 32) | h2


def resource_id(name: str) -> int:
    return murmur64b(name.encode('utf-8'))


def rerl_read(data: bytes):
    off, count = struct.unpack_from('<II', data, 0)
    out = []
    p = off
    for _ in range(count):
        rid = struct.unpack_from('<Q', data, p)[0]
        soff = struct.unpack_from('<i', data, p + 8)[0]
        s = p + 8 + soff
        name = data[s:data.index(b'\0', s)].decode('utf-8')
        out.append((rid, name)); p += 16
    return out


def rerl_write(names) -> bytes:
    names = list(dict.fromkeys(names))
    if not names:
        return struct.pack('<II', 0, 0)
    out = bytearray(struct.pack('<II', 8, len(names)))
    strings = bytearray(); start = len(names) * 16
    for i, name in enumerate(names):
        rel = start + len(strings) - (8 + i * 16)
        out += struct.pack('<Qii', resource_id(name), rel, 0)
        strings += name.encode('utf-8') + b'\0'
    return bytes(out + strings)


GUID_GENERIC = uuid.UUID('7412167c-06e9-4698-aff2-e63eb59037e7').bytes_le

# ---------------------------------------------------------------- textures

VTEX_RGBA8888 = 4


def _srgb_to_linear(c):
    c = c / 255.0
    return (c / 12.92) * (c <= 0.04045) + (((c + 0.055) / 1.055) ** 2.4) * (c > 0.04045)


def vtex_build(img, red2: bytes) -> bytes:
    """RGBA8888 texture with full mip chain and the DXT5 32x32 fallback thumbnail official files carry."""
    import numpy as np
    import quicktex, quicktex.s3tc.bc3 as bc3
    from PIL import Image
    img = img.convert('RGBA')
    w, h = img.size
    mips = [img]
    while mips[-1].size != (1, 1):
        mw, mh = mips[-1].size
        mips.append(mips[-1].resize((max(1, mw // 2), max(1, mh // 2)), Image.BOX))
    thumb = img.resize((32, 32), Image.BOX)
    fallback = bytes(bc3.BC3Encoder(5).encode(quicktex.RawTexture.frombytes(thumb.tobytes(), 32, 32)))
    assert len(fallback) == 1024
    lin = _srgb_to_linear(np.asarray(img.convert('RGB'), float)).reshape(-1, 3).mean(0)
    data = struct.pack('<HH4fHHHBBI', 1, 0, float(lin[0]), float(lin[1]), float(lin[2]), 0.0,
                       w, h, 1, VTEX_RGBA8888, len(mips), 0)
    data += struct.pack('<II', 8, 1)                   # extra data table follows immediately, 1 entry
    data += struct.pack('<III', 1, 8, len(fallback))   # FALLBACK_BITS, data right after this header
    data += fallback
    res = Resource(1, [['RED2', red2], ['DATA', data]]).build()
    pixels = b''.join(m.tobytes() for m in reversed(mips))   # smallest mip first
    return res + pixels

# ---------------------------------------------------------------- VPK v2 (single dir file)

def vpk_build(files: dict) -> bytes:
    """files: {'path/name.ext': bytes}. Everything is stored inside the _dir.vpk."""
    import hashlib, zlib
    tree = {}
    for full, data in files.items():
        full = full.replace('\\', '/').lower()
        folder, _, fname = full.rpartition('/')
        name, _, ext = fname.rpartition('.')
        tree.setdefault(ext, {}).setdefault(folder or ' ', []).append((name, data))
    t = bytearray(); blob = bytearray()
    for ext in sorted(tree):
        t += ext.encode() + b'\0'
        for folder in sorted(tree[ext]):
            t += folder.encode() + b'\0'
            for name, data in sorted(tree[ext][folder]):
                t += name.encode() + b'\0'
                t += struct.pack('<IHHIIH', zlib.crc32(data), 0, 0x7FFF, len(blob), len(data), 0xFFFF)
                blob += data
            t += b'\0'
        t += b'\0'
    t += b'\0'
    header = struct.pack('<7I', 0x55AA1234, 2, len(t), len(blob), 0, 48, 0)
    body = header + bytes(t) + bytes(blob)
    tree_md5 = hashlib.md5(bytes(t)).digest()
    chunk_md5 = hashlib.md5(b'').digest()
    file_md5 = hashlib.md5(body + tree_md5 + chunk_md5).digest()
    return body + tree_md5 + chunk_md5 + file_md5

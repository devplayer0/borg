import struct
import os
import tempfile

from . import BaseTestCase
from ..archive import ThinObjectProcessors
from ..cache import ChunkListEntry
from ..chunker import Chunk
from ..constants import *
from ..helpers import lvm

def zeros(n):
    return b'\0'*n
def zns(n):
    return zeros(n*4)

def make_deltas(*deltas):
    for d in deltas:
        yield lvm.Delta(*d)

tmap = {'h': 'hole', 'n': 'new', 'o': 'old'}
def gen_smap(*segs):
    i = 0
    smap = []
    for t, length in segs:
        smap.append((i, length, tmap[t]))
        i += length
    return smap

def gen_chunks(src, *splits):
    copy = bytearray(src)
    for s in splits:
        data = bytes(copy[:s*4])
        if data == zeros(len(data)):
            yield Chunk(None, size=len(data), allocation=CH_HOLE)
        else:
            yield Chunk(data, size=len(data), allocation=CH_DATA)
        copy = copy[s*4:]
    assert not copy

def check_alignment(segmap, t, chunks):
    it = iter(chunks)
    for (_, length, t2) in segmap:
        if t2 != t:
            continue

        i = 0
        while i < length*4:
            chunk = next(it)
            if isinstance(chunk, ChunkListEntry):
                size = chunk.size
            else:
                size = chunk.meta['size']
            i += size
            assert i <= length*4

        assert next(it) == None

class MockedFetcher:
    def __init__(self, fetch):
        self.fetch = fetch

    def __getattr__(self, n):
        match n:
            case 'archive' | 'pipeline':
                return self
            case 'fetch_many':
                return self.fetch
            case _:
                raise AttributeError(n)

def unpack_data(data):
    return struct.unpack('>' + 'I'*(len(data)//4), data)

class ThinTestCase(BaseTestCase):
    def setUp(self):
        self.i = 1
        self.cles = []
        self.m_fetcher = MockedFetcher(self.fetch_cles)

    def gen_data(self, n):
        data = struct.pack('>' + 'I'*n, *list(range(self.i, self.i+n)))
        self.i += n
        return data

    def gen_cles(self, src, *splits):
        cles = []
        for c in gen_chunks(src, *splits):
            cles.append(ChunkListEntry(len(self.cles), c.meta['size']))
            data = c.data
            if data is None:
                # zeros
                data = c.meta['size']
            self.cles.append(data)
        return cles

    def collapse_chunks(self, chunks):
        data = bytearray()
        for c in chunks:
            if c is None:
                continue

            if isinstance(c, ChunkListEntry):
                cle = self.cles[c.id]
                if isinstance(cle, int):
                    cle = zeros(cle)
                data += cle
                continue

            alloc = c.meta['allocation']
            if alloc == CH_DATA:
                assert c.meta['size'] == len(c.data)
                data += c.data
            elif alloc in (CH_HOLE, CH_ALLOC):
                assert c.data == None
                data += zeros(c.meta['size'])
            else:
                assert False, f'unknown allocation type {alloc}'
        return bytes(data)

    def compare(self, chunks, data):
        assert unpack_data(self.collapse_chunks(chunks)) == unpack_data(data)

    def fetch_cles(self, to_fetch, **kwargs):
        for i in to_fetch:
            cle = self.cles[i]
            if isinstance(cle, int):
                yield zeros(cle)
            else:
                yield cle

    def test_segmap(self):
        # 1: straight forward regular mapping
        delta = make_deltas(
            ('right_only', 3, 2),
            ('same', 7, 4),
            ('left_only', 15, 2),
            ('different', 20, 3),
        )
        assert list(ThinObjectProcessors._segmap_for_delta(total_blocks=25, delta=delta)) == [
            (0, 3, 'hole'),
            (3, 2, 'new'),
            (5, 2, 'hole'),
            (7, 4, 'old'),
            (11, 4, 'hole'),
            (15, 2, 'hole'),
            (17, 3, 'hole'),
            (20, 3, 'new'),
            (23, 2, 'hole'),
        ]

        # 2: mapping which partially stretches beyond the end
        delta = make_deltas(
            ('right_only', 3, 2),
            ('left_only', 7, 2),
            ('different', 11, 3),
        )
        assert list(ThinObjectProcessors._segmap_for_delta(total_blocks=12, delta=delta)) == [
            (0, 3, 'hole'),
            (3, 2, 'new'),
            (5, 2, 'hole'),
            (7, 2, 'hole'),
            (9, 2, 'hole'),
            (11, 1, 'new'),
        ]

        # 3: mapping which is completely beyond the end
        delta = make_deltas(
            ('right_only', 3, 2),
            ('left_only', 7, 2),
            ('different', 15, 3),
        )
        assert list(ThinObjectProcessors._segmap_for_delta(total_blocks=11, delta=delta)) == [
            (0, 3, 'hole'),
            (3, 2, 'new'),
            (5, 2, 'hole'),
            (7, 2, 'hole'),
            (9, 2, 'hole'),
        ]

    def test_dense_delta(self):
        with tempfile.TemporaryFile(prefix='borgthin', mode='w+b', buffering=0) as f:
            segmap = gen_smap(('h', 5), ('n', 3), ('o', 2), ('n', 8), ('h', 4))
            ex_in = zns(5) + self.gen_data(13) + self.gen_data(4)
            f.write(ex_in)
            f.seek(0)
            ddf = ThinObjectProcessors.DenseDeltaFile(segmap=segmap, block_size=4, fd=f.fileno())

            self.i = 1
            def compare(n, ex):
                assert unpack_data(ddf.read(n*4)) == unpack_data(ex)

            compare(2, self.gen_data(2))

            ex = self.gen_data(1)
            self.gen_data(2)
            ex += self.gen_data(4)
            compare(5, ex)

            compare(4, self.gen_data(4))

            assert not ddf.read(4)

    def test_new_chunks(self):
        ex = self.gen_data(100)

        # 1: simple case where chunks map 1:1 to segments
        segmap = gen_smap(('h', 9), ('n', 10), ('n', 20), ('h', 69), ('n', 70))
        chunks = gen_chunks(ex, 10, 20, 70)
        result = list(ThinObjectProcessors._new_chunks_align(segmap=segmap, block_size=4, chunk_iter=chunks))
        check_alignment(segmap, 'new', result)
        self.compare(result, ex)

        # 2: out of alignment
        segmap = gen_smap(('h', 9), ('n', 10), ('n', 5), ('h', 69), ('n', 20), ('n', 15), ('n', 50))
        chunks = gen_chunks(ex, 3, 7, 15, 10, 65)
        result = list(ThinObjectProcessors._new_chunks_align(segmap=segmap, block_size=4, chunk_iter=chunks))
        check_alignment(segmap, 'new', result)
        self.compare(result, ex)

    def test_old_chunks_simple(self):
        ex_in = zns(9) + self.gen_data(30) + zns(69) + self.gen_data(70)
        self.i = 1
        ex = self.gen_data(100)

        # 1: simple case where chunks map 1:1 to segments
        segmap = gen_smap(('h', 9), ('o', 10), ('o', 20), ('h', 69), ('o', 70))
        chunks = self.gen_cles(ex_in, 9, 10, 20, 69, 70)
        result = list(ThinObjectProcessors._old_chunks_filter_and_align(self.m_fetcher, segmap=segmap, block_size=4, chunks=chunks))
        check_alignment(segmap, 'old', result)
        self.compare(result, ex)

    def test_old_chunks(self):
        ex_in = zns(9) + self.gen_data(15) + zns(50) + self.gen_data(35) + zns(19) + self.gen_data(4) + zns(5) + self.gen_data(6) + zns(8) + self.gen_data(23) + zns(8) + self.gen_data(17)
        self.i = 1
        ex = self.gen_data(100)

        # 2: out of alignment
        segmap = gen_smap(('h', 9), ('o', 10), ('o', 5), ('h', 50), ('o', 20), ('o', 15), ('h', 19), ('o', 4), ('h', 5), ('o', 6), ('h', 8), ('o', 23), ('h', 8), ('o', 17))
        chunks = self.gen_cles(ex_in, 7, 2, 3, 7, 15, 10, 40, 5, 3, 16, 20, 19, 7, 11, 7, 16, 8, 10)
        for cle in chunks:
            data = self.cles[cle.id]
            if isinstance(data, int):
                continue
            print(f'chunk {cle.id}: {unpack_data(data)}')

        result = list(ThinObjectProcessors._old_chunks_filter_and_align(self.m_fetcher, segmap=segmap, block_size=4, chunks=chunks))
        check_alignment(segmap, 'old', result)
        self.compare(result, ex)

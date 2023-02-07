import string
from contextlib import contextmanager
import struct
import hashlib
import random
import fcntl
import os
import os.path
import subprocess

from ...helpers import lvm
from .. import changedir
from . import (
    ArchiverTestCaseBase,
    ArchiverTestCaseBinaryBase,
    RemoteArchiverTestCaseBase,
    RK_ENCRYPTION,
    BORG_EXES,
)

def make_id():
    return ''.join(random.choice(string.ascii_letters) for _ in range(8))

block_size = 4096
def write_random_data(f, size=2 * 1024 * 1024, keep=False):
    assert size % block_size == 0

    h = hashlib.sha256()
    data_all = bytearray()
    with open('/dev/urandom', 'rb') as r:
        while size > 0:
            data = r.read(block_size)
            h.update(data)
            f.write(data)
            if keep:
                data_all += data
            size -= block_size

    if keep:
        return data_all, h.hexdigest()
    return h.hexdigest()

def get_sum(f, size=None):
    if size is None:
        pos = f.tell()
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(pos, os.SEEK_SET)
    assert size % block_size == 0

    h = hashlib.sha256()
    while size > 0:
        h.update(f.read(block_size))
        size -= block_size

    return h.hexdigest()

IOCTL_BLKDISCARD = 0x1277
def ioctl(fd, req, fmt, *args):
    buf = struct.pack(fmt, *(args or [0]))
    buf = fcntl.ioctl(fd, req, buf)
    return struct.unpack(fmt, buf)[0]

def discard_chunk(fd, offset, size):
    ioctl(fd, IOCTL_BLKDISCARD, 'LL', offset, size)

class BaseThinTestCase(ArchiverTestCaseBase):
    chunk_size = 64 * 1024

    @contextmanager
    def make_vg(self, *args, size=512 * 1024 * 1024):
        id_ = make_id()

        image_file = os.path.join(self.tmpdir, f'pv-{id_}.img')
        with open(image_file, 'wb') as f:
            f.truncate(size)

        pv_dev = subprocess.check_output(['losetup', '-f', '--show', image_file]).strip()
        subprocess.check_call(['pvcreate', pv_dev])

        name = f'test-{id_}'
        cmd = ['vgcreate', name, pv_dev]
        cmd += args
        subprocess.check_call(cmd)

        try:
            yield name
        finally:
            subprocess.check_call(['vgremove', '-y', name])
            subprocess.check_call(['pvremove', pv_dev])
            subprocess.check_call(['losetup', '-d', pv_dev])

    def make_tpool(self, vg, size='256M'):
        name = f'tpool-{make_id()}'
        lvm.create(
            name, '--type', 'thin-pool',
            # using nopassdown should allow processing of discards independent of underlying medium
            '-Zn', '--chunksize', f'{self.chunk_size}B', '--discards', 'nopassdown',
            '-L', size, vg)
        return name

    def make_thin(self, vg, pool, size='128M'):
        name = f'thin-{make_id()}'
        lvm.create(name, '-V', size, '--thinpool', pool, vg)
        return lvm.get_lvs(f'{vg}/{name}')[0]

    def check_backup_sum(self, thin, arch, sum_, hook=None):
        with changedir(self.output_path):
            self.cmd(f'--repo={self.repository_location}', 'extract', '--sparse', '--noxattrs', arch)
            with open(thin['lv_full_name'], 'rb') as f:
                if hook is not None:
                    hook(f)
                assert get_sum(f) == sum_
            os.unlink(thin['lv_full_name'])

class CreateThinTestCase(BaseThinTestCase):
    def test_basic(self):
        self.cmd(f'--repo={self.repository_location}', 'rcreate', RK_ENCRYPTION)

        with self.make_vg() as vg:
            pool = self.make_tpool(vg)
            thin = self.make_thin(vg, pool)

            with open(thin['lv_path'], 'wb') as v:
                v.seek(4 * 1024 * 1024)
                write_random_data(v)

            # 1: try a simple backup of a new volume with some data allocated
            self.cmd(f'--repo={self.repository_location}', 'tcreate', 'myarch', thin['lv_full_name'])
            assert lvm.get_lvs(thin['lv_full_name'])
            assert not lvm.get_lvs(thin['lv_full_name'] + '_next')
            assert lvm.get_lvs(thin['lv_full_name'] + '_last')

            # 2: try a backup with more than one thin lv
            thin2 = self.make_thin(vg, pool)
            self.cmd(f'--repo={self.repository_location}', 'tcreate', 'myarch2', thin['lv_full_name'], thin2['lv_full_name'])
            assert lvm.get_lvs(thin2['lv_full_name'])
            assert not lvm.get_lvs(thin2['lv_full_name'] + '_next')
            assert lvm.get_lvs(thin2['lv_full_name'] + '_last')

    def test_content(self):
        self.cmd(f'--repo={self.repository_location}', 'rcreate', RK_ENCRYPTION)

        with self.make_vg() as vg:
            pool = self.make_tpool(vg)
            thin = self.make_thin(vg, pool, size='32M')

            # 1: check the vol is backed up correctly from scratch
            with open(thin['lv_path'], 'r+b') as v:
                v.seek(4 * 1024 * 1024)
                data_sum = write_random_data(v)

                v.seek(0)
                whole_sum = get_sum(v)
                print(whole_sum)

            output = self.cmd(f'--repo={self.repository_location}', '--debug', 'tcreate', 'first', thin['lv_full_name'])
            assert 'backing up from scratch' in output
            assert lvm.get_lvs(thin['lv_full_name'])
            assert not lvm.get_lvs(thin['lv_full_name'] + '_next')
            assert lvm.get_lvs(thin['lv_full_name'] + '_last')

            self.check_backup_sum(thin, 'first', whole_sum)

            # 2: check the vol is backed up correctly with a delta in new and unallocated areas
            with open(thin['lv_path'], 'r+b') as v:
                v.seek(4 * 1024 * 1024 + 2048)
                change = b'blahblahblah'
                v.write(change)

                v.seek(12 * 1024 * 1024)
                v.write(change)

                v.seek(0)
                whole_sum2 = get_sum(v)
                assert whole_sum2 != whole_sum
                print(whole_sum2)

            output = self.cmd(f'--repo={self.repository_location}', '--debug', 'tcreate', 'second', thin['lv_full_name'])
            assert 'backing up from scratch' not in output
            assert lvm.get_lvs(thin['lv_full_name'])
            assert not lvm.get_lvs(thin['lv_full_name'] + '_next')
            assert lvm.get_lvs(thin['lv_full_name'] + '_last')

            self.check_backup_sum(thin, 'second', whole_sum2)

            # 3: check the vol is backed up correctly with a discard in a previously allocated area
            with open(thin['lv_path'], 'r+b') as v:
                discard_chunk(v.fileno(), 4 * 1024 * 1024 + 65536, self.chunk_size)

                whole_sum3 = get_sum(v)
                assert whole_sum3 != whole_sum2
                print(whole_sum3)

            output = self.cmd(f'--repo={self.repository_location}', '--debug', 'tcreate', 'third', thin['lv_full_name'])
            assert 'backing up from scratch' not in output

            self.check_backup_sum(thin, 'third', whole_sum3)

    def test_resize(self):
        self.cmd(f'--repo={self.repository_location}', 'rcreate', RK_ENCRYPTION)

        with self.make_vg() as vg:
            pool = self.make_tpool(vg)
            thin = self.make_thin(vg, pool, size='32M')

            # 1: check the vol is backed up correctly from scratch
            with open(thin['lv_path'], 'r+b') as v:
                v.seek(6 * 1024 * 1024)
                write_random_data(v)

                v.seek(0)
                whole_sum = get_sum(v)
                print(whole_sum)

            output = self.cmd(f'--repo={self.repository_location}', '--debug', 'tcreate', 'first', thin['lv_full_name'])
            assert 'backing up from scratch' in output

            # 2: check the vol is backed up correctly with new data after growing
            subprocess.check_call(['lvresize', '-L', '+4M', thin['lv_full_name']])

            with open(thin['lv_path'], 'r+b') as v:
                v.seek(33 * 1024 * 1024)
                write_random_data(v, size=2*self.chunk_size)

                v.seek(0)
                whole_sum2 = get_sum(v)
                assert whole_sum2 != whole_sum
                print(whole_sum2)

            output = self.cmd(f'--repo={self.repository_location}', '--debug', 'tcreate', 'second', thin['lv_full_name'])
            assert 'backing up from scratch' not in output

            def check_size(size):
                def hook(f):
                    f.seek(0, os.SEEK_END)
                    assert f.tell() == size
                    f.seek(0)
                return hook
            self.check_backup_sum(thin, 'second', whole_sum2, hook=check_size(36 * 1024 * 1024))

            # 3: check the vol is backed up correctly with new data after shrinking
            subprocess.check_call(['lvresize', '-y', '-L', '-8M', thin['lv_full_name']])

            with open(thin['lv_path'], 'r+b') as v:
                v.seek(6 * 1024 * 1024 + 2048)
                write_random_data(v, size=3*self.chunk_size)

                v.seek(0)
                whole_sum3 = get_sum(v)
                assert whole_sum3 != whole_sum2
                assert whole_sum3 != whole_sum
                print(whole_sum3)

            output = self.cmd(f'--repo={self.repository_location}', '--debug', 'tcreate', 'third', thin['lv_full_name'])
            assert 'backing up from scratch' not in output

            self.check_backup_sum(thin, 'third', whole_sum3, hook=check_size(28 * 1024 * 1024))

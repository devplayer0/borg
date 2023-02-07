import string
from contextlib import contextmanager
import hashlib
import random
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

def get_sum(f, size):
    assert size % block_size == 0

    h = hashlib.sha256()
    while size > 0:
        h.update(f.read(block_size))
        size -= block_size

    return h.hexdigest()

class BaseThinTestCase(ArchiverTestCaseBase):
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
        lvm.create(name, '--type', 'thin-pool', '-Zn', '-L', size, vg)
        return name

    def make_thin(self, vg, pool, size='128M'):
        name = f'thin-{make_id()}'
        lvm.create(name, '-V', size, '--thinpool', pool, vg)
        return lvm.get_lvs(f'{vg}/{name}')[0]

class CreateThinTestCase(BaseThinTestCase):
    def test_basic(self):
        self.cmd(f'--repo={self.repository_location}', 'rcreate', RK_ENCRYPTION)

        with self.make_vg() as vg:
            pool = self.make_tpool(vg)
            thin = self.make_thin(vg, pool)

            with open(thin['lv_path'], 'wb') as v:
                v.seek(4 * 1024 * 1024)
                write_random_data(v)

            self.cmd(f'--repo={self.repository_location}', 'tcreate', 'myarch', thin['lv_full_name'])
            assert lvm.get_lvs(thin['lv_full_name'])
            assert not lvm.get_lvs(thin['lv_full_name'] + '_next')
            assert lvm.get_lvs(thin['lv_full_name'] + '_last')

    def test_content(self):
        self.cmd(f'--repo={self.repository_location}', 'rcreate', RK_ENCRYPTION)

        with self.make_vg() as vg:
            pool = self.make_tpool(vg)
            thin = self.make_thin(vg, pool, size='32M')

            whole_size = 32 * 1024 * 1024
            with open(thin['lv_path'], 'r+b') as v:
                v.seek(4 * 1024 * 1024)
                data, data_sum = write_random_data(v, keep=True)

                v.seek(0)
                whole_sum = get_sum(v, whole_size)
                print(whole_sum)

            output = self.cmd(f'--repo={self.repository_location}', '--debug', 'tcreate', 'first', thin['lv_full_name'])
            assert 'backing up from scratch' in output
            assert lvm.get_lvs(thin['lv_full_name'])
            assert not lvm.get_lvs(thin['lv_full_name'] + '_next')
            assert lvm.get_lvs(thin['lv_full_name'] + '_last')

            with changedir(self.output_path):
                self.cmd(f'--repo={self.repository_location}', 'extract', '--sparse', '--noxattrs', 'first')
                with open(thin['lv_full_name'], 'rb') as f:
                    assert get_sum(f, whole_size) == whole_sum

                os.unlink(thin['lv_full_name'])


            with open(thin['lv_path'], 'r+b') as v:
                v.seek(4 * 1024 * 1024 + 2048)
                change = b'blahblahblah'
                v.write(change)

                v.seek(12 * 1024 * 1024)
                v.write(change)

                v.seek(0)
                whole_sum2 = get_sum(v, whole_size)
                print(whole_sum2)

            output = self.cmd(f'--repo={self.repository_location}', '--debug', 'tcreate', 'second', thin['lv_full_name'])
            assert 'backing up from scratch' not in output
            assert lvm.get_lvs(thin['lv_full_name'])
            assert not lvm.get_lvs(thin['lv_full_name'] + '_next')
            assert lvm.get_lvs(thin['lv_full_name'] + '_last')

            with changedir(self.output_path):
                self.cmd(f'--repo={self.repository_location}', 'extract', '--sparse', '--noxattrs', 'second')
                with open(thin['lv_full_name'], 'rb') as f:
                    assert get_sum(f, whole_size) == whole_sum2

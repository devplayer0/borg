from contextlib import contextmanager
from enum import Enum
import json
import xml.etree.ElementTree
import subprocess

def get_size(info, k):
    return int(info[k][:-1])

def get_lvs(spec=None, select=None, uuid=None):
    cmd = ['lvs', '-a', '-olv_all,seg_all', '--units=b', '--reportformat', 'json_std']

    if spec is not None:
        cmd.append(spec)

    assert not (select and uuid)
    if select is not None:
        cmd += ['--select', select]
    elif uuid is not None:
        cmd += ['--select', f'lv_uuid={uuid}']

    output = subprocess.check_output(cmd)
    return json.loads(output)['report'][0]['lv']

def reserve_meta_snapshot(path, activate=True):
    action = 'reserve' if activate else 'release'
    subprocess.check_call(['dmsetup', 'message', path, '0', f'{action}_metadata_snap'])

@contextmanager
def meta_snapshot(path):
    ret = reserve_meta_snapshot(path, activate=True)
    try:
        yield ret
    finally:
        reserve_meta_snapshot(path, activate=False)

def create(name, *params):
    cmd = ['lvcreate', '-qq', '-n', name, '--addtag=borgthin']
    cmd += params
    subprocess.check_call(cmd)

def rename(vg, old, new):
    subprocess.check_call(['lvrename', '-qq', vg, old, new])

def remove(uuid):
    subprocess.check_call(['lvremove', '-qq', '-y', '--select', f'lv_uuid={uuid}'])

class Delta:
    Type = Enum('DeltaType', ['LEFT_ONLY', 'RIGHT_ONLY', 'DIFFERENT', 'SAME'])

    def __init__(self, t, begin, length):
        match t:
            case 'left_only':
                self.type = self.Type.LEFT_ONLY
            case 'right_only':
                self.type = self.Type.RIGHT_ONLY
            case 'different':
                self.type = self.Type.DIFFERENT
            case 'same':
                self.type = self.Type.SAME
            case _:
                assert False, f'Invalid delta type {t}'
        self.begin = begin
        self.length = length

def thin_delta(meta_path, thin1, thin2):
    cmd = ['thin_delta', '--metadata-snap', '--thin1', str(thin1), '--thin2', str(thin2), meta_path]
    output = subprocess.check_output(cmd)
    superblock = xml.etree.ElementTree.fromstring(output)

    assert int(superblock[0].attrib['left']) == thin1 and int(superblock[0].attrib['right']) == thin2
    for info in superblock[0]:
        yield Delta(info.tag, int(info.attrib['begin']), int(info.attrib['length']))

def thin_dump(meta_path, thin_id):
    cmd = ['thin_dump', '--metadata-snap', '--dev-id', str(thin_id), meta_path]
    output = subprocess.check_output(cmd)
    superblock = xml.etree.ElementTree.fromstring(output)

    for info in superblock[0]:
        match info.tag:
            case 'single_mapping':
                begin = int(info.attrib['origin_block'])
                length = 1
            case 'range_mapping':
                begin = int(info.attrib['origin_begin'])
                length = int(info.attrib['length'])
            case _:
                assert False, f'Unknown thin mapping type {info.tag}'
        yield Delta('right_only', begin, length)

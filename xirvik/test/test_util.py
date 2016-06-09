from __future__ import print_function
from hashlib import sha1
from io import BytesIO as StringIO
from os import close as close_fd, remove as rm, rmdir, write as write_fd
from os.path import basename, dirname
from random import SystemRandom
from tempfile import mkdtemp, mkstemp
import struct
import sys
import unittest

from bencodepy import encode as bencode

from xirvik.util import VerificationError, verify_torrent_contents


random = SystemRandom()


def create_random_data(size):
    return bytearray(random.getrandbits(8) for _ in range(size))


class TempFilesMixin(object):
    _temp_files = []

    def tearDown(self):
        for x in self._temp_files:
            try:
                rm(x)
            except IOError as e:
                if e.errno == 2:
                    continue
                else:
                    print(str(e), file=sys.stderr)

    def _mktemp(self, contents=None, prefix='test-', dir=None):
        fd, name = mkstemp(prefix=prefix, dir=dir)
        write_fd(fd, contents)
        close_fd(fd)

        self._temp_files.append(name)

        return name


class TestTorrentVerfication(TempFilesMixin, unittest.TestCase):
    FILE_SIZE = 2509
    PIECE_LENGTH = 256

    file1 = None
    file2 = None
    torrent_data = None
    torrent_data_path = None
    torrent_data_dict = None
    torrent_name = None

    def setUp(self):
        """ A torrent generator! """
        self.torrent_data_path = mkdtemp(prefix='test-torrent-verification-')
        self.torrent_name = basename(self.torrent_data_path)

        all_data = create_random_data(self.FILE_SIZE * 2)
        pieces = b''

        self.file1 = self._mktemp(contents=all_data[0:self.FILE_SIZE],
                                  dir=self.torrent_data_path)
        self.file2 = self._mktemp(contents=all_data[self.FILE_SIZE:],
                                  dir=self.torrent_data_path)

        for i in range(0, self.FILE_SIZE * 2, self.PIECE_LENGTH):
            s = sha1()
            s.update(all_data[i:i + self.PIECE_LENGTH])
            pieces += s.digest()

        self.torrent_data_dict = {
            b'announce': 'https://fake.com',
            b'info': {
                b'name': self.torrent_name,
                b'piece length': self.PIECE_LENGTH,
                b'pieces': pieces,
                b'files': [
                    {
                        b'length': self.FILE_SIZE,
                        b'path': [basename(self.file1)],
                    },
                    {
                        b'length': self.FILE_SIZE,
                        b'path': [basename(self.file2)],
                    },
                ],
            }
        }
        self.torrent_data = bencode(self.torrent_data_dict)

        self.torrent_file_path = self._mktemp(contents=self.torrent_data)

    def tearDown(self):
        super(TestTorrentVerfication, self).tearDown()
        rmdir(self.torrent_data_path)

    def test_verify_torrent_contents_string(self):
        verify_torrent_contents(self.torrent_data,
                                dirname(self.torrent_data_path))

    def test_verify_torrent_contents_filepath(self):
        verify_torrent_contents(self.torrent_file_path,
                                dirname(self.torrent_data_path))

    def test_verify_torrent_contents_stringio(self):
        verify_torrent_contents(StringIO(self.torrent_data),
                                dirname(self.torrent_data_path))

    def test_verify_torrent_contents_invalid_path(self):
        with self.assertRaises(IOError):
            verify_torrent_contents(self.torrent_data,
                                    dirname(self.torrent_data_path) + 'junk')

    def test_verify_torrent_contents_file_missing(self):
        rm(self.file2)
        with self.assertRaises(VerificationError):
            verify_torrent_contents(self.torrent_data,
                                    dirname(self.torrent_data_path))

    def test_verify_torrent_contents_keyerror(self):
        del self.torrent_data_dict[b'info'][b'files'][0][b'path']
        self.torrent_data = bencode(self.torrent_data_dict)

        with self.assertRaises(KeyError):
            verify_torrent_contents(self.torrent_data,
                                    dirname(self.torrent_data_path))

    def test_verify_torrent_contents_keyerror2(self):
        del self.torrent_data_dict[b'info']
        self.torrent_data = bencode(self.torrent_data_dict)

        with self.assertRaises(KeyError):
            verify_torrent_contents(self.torrent_data,
                                    dirname(self.torrent_data_path))

    def test_verify_torrent_contents_bad_compare(self):
        with open(self.file2, 'w') as f:
            f.write('junk\n')

        with self.assertRaises(VerificationError):
            verify_torrent_contents(self.torrent_file_path,
                                    dirname(self.torrent_data_path))


class TestSingleFileTorrentVerification(TempFilesMixin, unittest.TestCase):
    FILE_SIZE = 2509
    PIECE_LENGTH = 256

    file1 = None
    torrent_data = None
    torrent_data_path = None
    torrent_data_dict = None
    torrent_name = None

    def setUp(self):
        all_data = create_random_data(self.FILE_SIZE)
        self.file1 = self._mktemp(contents=all_data)
        self.torrent_data_path = dirname(self.file1)

        pieces = b''
        for i in range(0, self.FILE_SIZE, self.PIECE_LENGTH):
            s = sha1()
            s.update(all_data[i:i + self.PIECE_LENGTH])
            pieces += s.digest()

        self.torrent_data_dict = {
            b'announce': 'https://fake.com',
            b'info': {
                b'name': self.file1,
                b'piece length': self.PIECE_LENGTH,
                b'pieces': pieces,
            }
        }
        self.torrent_data = bencode(self.torrent_data_dict)

        self.torrent_file_path = self._mktemp(contents=self.torrent_data)

    def test_verify_torrent_contents_string(self):
        verify_torrent_contents(self.torrent_data, self.torrent_data_path)

    def test_verify_torrent_contents_filepath(self):
        verify_torrent_contents(self.torrent_file_path,
                                self.torrent_data_path)

    def test_verify_torrent_contents_stringio(self):
        verify_torrent_contents(StringIO(self.torrent_data),
                                self.torrent_data_path)

    def test_verify_torrent_contents_file_missing(self):
        rm(self.file1)
        with self.assertRaises(IOError):
            verify_torrent_contents(self.torrent_data, self.torrent_data_path)

    def test_verify_torrent_contents_bad_compare(self):
        with open(self.file1, 'w') as f:
            f.write('junk\n')

        with self.assertRaises(VerificationError):
            verify_torrent_contents(self.torrent_file_path,
                                    self.torrent_data_path)


if __name__ == '__main__':
    unittest.main()

"""SFTP client like paramiko's with extra features."""
from __future__ import print_function
from datetime import datetime
from math import ceil, floor
from os import chmod, makedirs, utime
from os.path import basename, dirname, isdir, join as path_join, realpath
import inspect
import os
import logging
import socket

from humanize import naturaldelta, naturalsize
from paramiko.client import SSHClient
from paramiko.sftp import SFTPError
from paramiko import SFTPFile

__all__ = (
    'SFTPClient',
    'LOG_NAME',
)


LOG_NAME = 'xirvik.sftp'
LOG_INTERVAL = 60


class SFTPClient(object):
    """Dynamic extension on paramiko's SFTPClient."""

    MAX_PACKET_SIZE = SFTPFile.__dict__['MAX_REQUEST_SIZE']

    ssh_client = None
    client = None
    raise_exceptions = False
    original_arguments = {}
    debug = False

    _log = logging.getLogger(LOG_NAME)
    _dircache = []

    def __init__(self, **kwargs):
        """Constructor."""
        self.original_arguments = kwargs.copy()
        self._connect(**kwargs)

    def __enter__(self):
        """For use with a with statement."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """For use with a with statement."""
        self.close_all()

    def _connect(self, **kwargs):
        kwargs_to_paramiko = dict(
            look_for_keys=kwargs.pop('look_for_keys', True),
            username=kwargs.pop('username'),
            port=kwargs.pop('port', 22),
            allow_agent=False,
            timeout=kwargs.pop('timeout', None),
        )
        host = kwargs.pop('hostname', 'localhost')
        password = kwargs.pop('password')
        keepalive = kwargs.pop('keepalive', 5)
        if password:
            kwargs_to_paramiko['password'] = password
        self.raise_exceptions = kwargs.pop('raise_exceptions', False)

        self.ssh_client = SSHClient()
        self.ssh_client.load_system_host_keys()
        self.ssh_client.connect(host, **kwargs_to_paramiko)

        self.client = self.ssh_client.open_sftp()
        channel = self.client.get_channel()
        channel.settimeout(kwargs_to_paramiko['timeout'])
        channel.get_transport().set_keepalive(keepalive)

        # 'Extend' the SFTPClient class
        is_reconnect = kwargs.pop('is_reconnect', False)
        members = inspect.getmembers(self.client,
                                     predicate=inspect.ismethod)
        self._log.debug('Dynamically adding methods from original SFTPClient')
        for (method_name, method) in members:
            if method_name[0:2] == '__' or method_name == '_log':
                self._log.debug('Ignorning {}()'.format(method_name))
                continue

            if not is_reconnect and hasattr(self, method_name):
                raise AttributeError('Not overwriting property "{}". This '
                                     'version of Paramiko is not '
                                     'supported.'.format(method_name))

            self._log.debug('Adding method {}()'.format(method_name))
            setattr(self, method_name, method)

    def close_all(self):
        """Close client and SSH client handles."""
        self.client.close()
        self.ssh_client.close()

    def clear_directory_cache(self):
        """Reset directory cache."""
        self._dircache = []

    def listdir_attr_recurse(self, path='.'):
        """List directory attributes recursively."""
        for da in self.client.listdir_attr(path=path):
            is_dir = da.st_mode & 0o700 == 0o700
            if is_dir:
                try:
                    for x in self.listdir_attr_recurse(
                            path_join(path, da.filename)):
                        yield x
                except IOError as e:
                    if self.raise_exceptions:
                        raise e
            else:
                yield (path_join(path, da.filename), da,)

    def _get_callback(self, start_time, _log):
        def cb(tx_bytes, total_bytes):
            total_time = datetime.now() - start_time
            total_time = total_time.total_seconds()
            total_time_s = floor(total_time)

            if (total_time_s % LOG_INTERVAL) != 0:
                return

            nsize_tx = naturalsize(tx_bytes,
                                   binary=True,
                                   format='%.2f')
            nsize_total = naturalsize(total_bytes,
                                      binary=True,
                                      format='%.2f')

            speed_in_s = tx_bytes / total_time
            speed_in_s = naturalsize(speed_in_s,
                                     binary=True,
                                     format='%.2f')

            _log.info('Downloaded {} / {} in {} ({}/s)'.format(
                nsize_tx,
                nsize_total,
                naturaldelta(datetime.now() - start_time),
                speed_in_s,
                total_time_s))

        return cb

    def mirror(self,
               path='.',
               destroot='.',
               keep_modes=True,
               keep_times=True,
               resume=True):
        """
        Mirror a remote directory to a local location.

        path is the remote directory. destroot must be the location where
        destroot/path will be created (the path must not already exist).

        keep_modes and keep_times are boolean to ensure permissions and time
        are retained respectively.

        Pass resume=False to disable file resumption.
        """
        n = 0
        resume_seek = None
        cwd = self.getcwd()

        for _path, info in self.listdir_attr_recurse(path=path):
            if info.st_mode & 0o700 == 0o700:
                continue

            dest_path = path_join(destroot, dirname(_path))
            dest = path_join(dest_path, basename(_path))

            if dest_path not in self._dircache:
                try:
                    makedirs(dest_path)
                except OSError:
                    pass
                self._dircache.append(dest_path)

            if isdir(dest):
                continue

            try:
                with open(dest, 'rb'):
                    current_size = os.stat(dest).st_size

                    if current_size != info.st_size:
                        resume_seek = current_size
                        if resume:
                            self._log.info('Resuming file {} at {} '
                                           'bytes'.format(dest, current_size))
                        raise IOError()  # ugly goto
            except IOError:
                while True:
                    try:
                        # Only size is used to determine complete-ness here
                        # Hash verification is in the util module
                        if resume_seek and resume:
                            read_tuples = []

                            n_reads = ceil((info.st_size - resume_seek) /
                                           self.MAX_PACKET_SIZE) - 1
                            n_left = ((info.st_size - resume_seek) %
                                      self.MAX_PACKET_SIZE)
                            offset = 0

                            for n in range(n_reads):
                                read_tuples.append((resume_seek + offset,
                                                    self.MAX_PACKET_SIZE,))
                                offset += self.MAX_PACKET_SIZE
                            read_tuples.append((resume_seek + offset, n_left,))

                            with self.client.open(_path) as rf:
                                with open(dest, 'ab') as f:
                                    f.seek(resume_seek)
                                    resume_seek = None

                                    for chunk in rf.readv(read_tuples):
                                        f.write(chunk)
                        else:
                            dest = realpath(dest)
                            self._log.info('Downloading {} -> '
                                           '{}'.format(_path, dest))

                            start_time = datetime.now()
                            self.client.get(_path, dest)

                            self._get_callback(start_time, self._log)(
                                info.st_size, info.st_size)

                        # Do not count files that were already downloaded
                        n += 1

                        break
                    except (socket.timeout, SFTPError) as e:
                        # Resume at position - 10 bytes
                        resume_seek = os.stat(dest).st_size - 10
                        if isinstance(e, socket.timeout):
                            self._log.error('Connection timed out')
                        else:
                            self._log.error('{!s}'.format(e))

                        if resume:
                            self._log.info('Resuming GET {} at {} '
                                           'bytes'.format(_path,
                                                          resume_seek))
                        else:
                            self._log.debug('Not resuming (resume = {}, '
                                            'exception: {})'.format(resume,
                                                                    e))
                            raise e

                        self._log.debug('Re-establishing connection')
                        self.original_arguments['is_reconnect'] = True
                        self._connect(**self.original_arguments)
                        if cwd:
                            self.chdir(cwd)

            # Okay to fix existing files even if they are already downloaded
            try:
                if keep_modes:
                    chmod(dest, info.st_mode)
                if keep_times:
                    utime(dest, (info.st_atime, info.st_mtime,))
            except IOError:
                pass

        return n

    def __str__(self):
        """Return string representation."""
        return '{} (wrapped by {}.SFTPClient)'.format(
            str(self.client), __name__)
    __unicode__ = __str__

from logging.handlers import SysLogHandler
from os.path import basename, expanduser, join as path_join, realpath
from netrc import netrc
from tempfile import gettempdir
import argparse
import hashlib
import json
import logging
import re
import signal
import sys

from lockfile import LockFile, NotLocked
import requests

from xirvik.client import (
    ruTorrentClient,
    UnexpectedruTorrentError,
    TORRENT_PATH_INDEX,
)
from xirvik.logging import cleanup, get_logger
from xirvik.sftp import SFTPClient
from xirvik.util import (
    cleanup_and_exit,
    ctrl_c_handler,
    verify_torrent_contents,
    VerificationError,
)

_lock = None

def lock_ctrl_c_handler(signum, frame):
    if _lock:
        try:
            _lock.release()
        except NotLocked:
            pass

    ctrl_c_handler(signum, frame)
    raise SystemExit('Signal raised')


def main():
    signal.signal(signal.SIGINT, lock_ctrl_c_handler)

    parser = argparse.ArgumentParser()

    parser.add_argument('-H', '--host', required=True)
    parser.add_argument('-P', '--port', type=int, default=22)
    parser.add_argument('-c', '--netrc-path', default=expanduser('~/.netrc'))
    parser.add_argument('-r', '--resume', action='store_true',
                        help='Resume incomplete files (experimental)')
    parser.add_argument('-T', '--move-to', required=True)
    parser.add_argument('-L', '--label', default='Seeding')
    parser.add_argument('-d', '--debug', action='store_true')
    parser.add_argument('-v', '--verbose', action='store_true')
    parser.add_argument('-s', '--syslog', action='store_true')
    parser.add_argument('--no-preserve-permissions', action='store_false')
    parser.add_argument('--no-preserve-times', action='store_false')
    parser.add_argument('--max-retries', type=int, default=10)
    parser.add_argument('remote_dir', metavar='REMOTEDIR', nargs=1)
    parser.add_argument('local_dir', metavar='LOCALDIR', nargs=1)

    args = parser.parse_args()
    verbose = args.debug or args.verbose
    log = get_logger('xirvik',
                     verbose=args.verbose,
                     debug=args.debug,
                     syslog=args.syslog)
    if args.debug:
        logs_to_follow = (
            'requests',
            'paramiko',
        )
        for name in logs_to_follow:
            _log = logging.getLogger(name)
            formatter = logging.Formatter('%(asctime)s - %(name)s - '
                                          '%(levelname)s - %(message)s')
            channel = logging.StreamHandler(sys.stderr)

            _log.setLevel(logging.DEBUG)
            channel.setLevel(logging.DEBUG)
            channel.setFormatter(formatter)
            _log.addHandler(channel)

    local_dir = realpath(args.local_dir[0])
    user, _, password = netrc(args.netrc_path).authenticators(args.host)
    sftp_host = 'sftp://{user:s}@{host:s}'.format(
        user=user,
        host=args.host,
    )

    lf_hash = hashlib.sha256(json.dumps(args._get_kwargs()).encode('utf-8')).hexdigest()
    lf_path = path_join(gettempdir(), 'xirvik-mirror-{}'.format(lf_hash))
    log.debug('Acquiring lock at {}.lock'.format(lf_path))
    _lock = LockFile(lf_path)
    _lock.acquire()
    log.debug('Lock acquired')

    log.debug('Local directory to sync to: {}'.format(local_dir))
    log.debug('Read user and password from netrc file')
    log.debug('SFTP URI: {}'.format(sftp_host))

    client = ruTorrentClient(args.host, user, password, max_retries=args.max_retries)

    http_prefix = 'https://{host:s}'.format(host=args.host)
    multirpc_action_uri = ('{}/rtorrent/plugins/multirpc/'
                           'action.php'.format(http_prefix))
    datadir_action_uri = ('{}/rtorrent/plugins/datadir/'
                          'action.php'.format(http_prefix))
    assumed_path_prefix = '/torrents/{}'.format(user)
    look_for = '{}/{}/'.format(assumed_path_prefix, args.remote_dir[0])
    move_to = '{}/{}'.format(assumed_path_prefix, args.move_to)
    names = {}

    log.debug('Full completed directory path name: {}'.format(look_for))
    log.debug('Moving finished torrents to: {}'.format(move_to))

    log.info('Getting current torrent information (ruTorrent)')
    try:
        torrents = client.list_torrents()
    except requests.exceptions.ConnectionError as e:
        # Assume no Internet connection at this point
        log.error('Failed to connect: {}'.format(e))
        try:
            _lock.release()
        except NotLocked:
            pass
        cleanup_and_exit(1)

    for hash, v in torrents.items():
        if not v[TORRENT_PATH_INDEX].startswith(look_for):
            continue
        bn = basename(v[TORRENT_PATH_INDEX])
        names[bn] = (hash, v[TORRENT_PATH_INDEX],)

        log.info('Completed torrent "{}" found with hash {}'.format(bn, hash,))

    sftp_client_args = dict(
        hostname=args.host,
        username=user,
        password=password,
        port=args.port,
    )

    try:
        with SFTPClient(**sftp_client_args) as sftp_client:
            log.info('Verifying contents of {} with previous '
                     'response'.format(look_for))

            sftp_client.chdir(args.remote_dir[0])
            for item in sftp_client.listdir_iter(read_aheads=10):
                if item.filename not in names:
                    log.error('File or directory "{}" not found in previous '
                            'response body'.format(item.filename))
                    continue

                log.debug('Found matching torrent "{}" from ls output'.format(item.filename))

            if not len(names.items()):
                log.info('Nothing found to mirror')
                _lock.release()
                cleanup_and_exit()

            sftp_client.mirror(destroot=local_dir,
                               resume=args.resume,
                               keep_modes=not args.no_preserve_permissions,
                               keep_times=not args.no_preserve_times)
    except Exception as e:
        if args.debug:
            _lock.release()
            cleanup()
            raise e
        else:
            log.error(str(e))
        _lock.release()
        cleanup_and_exit()

    _all = names.items()
    exit_status = 0
    bad = []
    for bn, (hash, fullpath) in _all:
        # There is a warning that can get raised here by urllib3 if
        # Content-Disposition header's filename field has any
        # non-ASCII characters. It is ignorable as the content still gets
        # downloaded correctly
        log.info('Verifying "{}"'.format(bn))
        r, _ = client.get_torrent(hash)
        try:
            verify_torrent_contents(r.content, local_dir)
        except VerificationError as e:
            log.error('Could not verify "{}" contents against piece hashes in torrent file'.format(bn))
            exit_status = 1
            bad.append(hash)

    # Move to _seeding directory and set label
    # Unfortunately, there is no method, via the API, to do this one HTTP
    #   request
    for bn, (hash, fullpath) in _all:
        if hash in bad:
            continue
        log.info('Moving "{}" to "{}" directory'.format(bn, move_to))
        try:
            client.move_torrent(hash, move_to)
        except UnexpectedruTorrentError as e:
            log.error(str(e))

    log.info('Setting label to "{}" for downloaded items'.format(args.label))

    client.set_label_to_hashes(hashes=[hash for bn, (hash, fullpath)
                                       in names.items() if hash not in bad],
                               label=args.label)

    if exit_status != 0:
        log.error('Could not verify torrent checksums')

    _lock.release()
    cleanup_and_exit(exit_status)

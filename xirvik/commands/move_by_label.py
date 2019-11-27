#!/usr/bin/env python
from time import sleep
from typing import Any, Callable, Dict, Optional, Tuple, Union
import argparse
import logging
import sys

from requests.exceptions import HTTPError
import argcomplete

from ..client import ruTorrentClient
from .util import setup_logging_stdout

PREFIX = '/torrents/{}/_completed'
log: Optional[logging.Logger] = None


def base_path_check(username: str
                    ) -> Callable[[Tuple[Any, Dict[str, str]]], bool]:
    def bpc(hi: Tuple[Any, Dict[str, str]]) -> bool:
        _, info = hi
        move_to = '{}/{}'.format(PREFIX.format(username),
                                 str.lower(info['custom1']))
        return not info['base_path'].startswith(move_to)

    return bpc


def key_check(hi: Tuple[Any, Dict[str, Union[bool, int]]]) -> bool:
    _, info = hi
    return not info['is_hash_checking'] and info['left_bytes'] == 0


def main() -> int:
    global log
    log = setup_logging_stdout()
    assert log is not None
    parser = argparse.ArgumentParser()
    parser.add_argument('-a', '--ignore-ratio', action='store_true')
    parser.add_argument('-u', '--username', required=False)
    parser.add_argument('-p', '--password', required=False)
    parser.add_argument('-r', '--max-retries', type=int, default=10)
    parser.add_argument('-c', '--completed-dir', default='_completed')
    parser.add_argument('-t', '--sleep-time', default=10, type=int)
    parser.add_argument('-l', '--lower-label', action='store_true')
    parser.add_argument('--netrc', required=False)
    parser.add_argument('--ignore-labels', nargs='+', default=[])
    parser.add_argument('host', nargs=1)
    argcomplete.autocomplete(parser)
    args = parser.parse_args()
    client = ruTorrentClient(args.host[0],
                             name=args.username,
                             password=args.password,
                             max_retries=args.max_retries,
                             netrc_path=args.netrc)
    username = client.name
    try:
        torrents = list(client.list_torrents_dict().items())
    except (ValueError, HTTPError):
        log.error('Connection failed on list_torrents() call')
        return 1
    count = 0
    assert username is not None
    hash: str
    info: Dict[str, str]
    for hash, info in list(
            filter(base_path_check(username), filter(key_check, torrents))):
        label = info['custom1']
        if label in args.ignore_labels:
            continue
        if args.lower_label:
            label = label.lower()
        move_to = '{}/{}'.format(PREFIX.format(username), label)
        log.info('Moving %s to %s/', info['name'], move_to)
        client.move_torrent(hash, move_to)
        count += 1
        if count > 0 and (count % 10) == 0:
            sleep(args.sleep_time)
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""Read-only service integrations. No deletion operations live here."""
import http.cookiejar
import json
import os
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path


def request_json(url, headers=None, opener=None):
    req = urllib.request.Request(url, headers=headers or {})
    with (opener or urllib.request).open(req, timeout=10) as response:
        return json.load(response)


def mount_status(path, expected_uuid):
    result = subprocess.run(['findmnt', '-n', '-o', 'UUID,TARGET', '--target', path], capture_output=True, text=True, check=True)
    fields = result.stdout.strip().split()
    return len(fields) == 2 and fields[0].lower() == expected_uuid.lower() and fields[1] == path


def inspect_torrent_files(root, torrent_files, expected_file):
    """Check every video file, reject traversal and require one exact inode match."""
    root = Path(root).resolve(strict=True)
    expected = Path(expected_file).resolve(strict=True)
    if not expected.is_file():
        return {'safe': False, 'reason': 'Movie file unavailable'}
    videos = []
    for f in torrent_files:
        name = f.get('name', '')
        part = Path(name)
        if part.is_absolute() or '..' in part.parts:
            return {'safe': False, 'reason': 'Unsafe torrent path'}
        if part.suffix.lower() in {'.mkv', '.mp4', '.avi', '.mov', '.m4v', '.ts', '.m2ts', '.wmv'}:
            videos.append(part)
    if len(videos) != 1:
        return {'safe': False, 'reason': 'Torrent contains zero or multiple video files'}
    try:
        actual = (root / videos[0]).resolve(strict=True)
        if not actual.is_relative_to(root) or not actual.is_file():
            raise ValueError('Outside download root')
        a, b = actual.stat(), expected.stat()
        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
            return {'safe': False, 'reason': 'File is not the same hard link'}
    except (OSError, ValueError):
        return {'safe': False, 'reason': 'Torrent file unavailable or outside download root'}
    return {'safe': True, 'reason': 'Exactly one video file, same inode', 'bytes': a.st_size}


class Qbit:
    def __init__(self, url, username, password):
        self.base = url.rstrip('/')
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        body = urllib.parse.urlencode({'username': username, 'password': password}).encode()
        req = urllib.request.Request(self.base + '/api/v2/auth/login', data=body)
        with self.opener.open(req, timeout=10) as r:
            result = r.read().decode().strip()
            if r.status not in (200, 204) or result.lower() not in ('', 'ok', 'ok.'):
                raise ValueError('qBittorrent authentication failed')

    def get(self, path):
        return request_json(self.base + '/api/v2/' + path, opener=self.opener)

    def delete_torrent(self, torrent_hash):
        if len(torrent_hash) not in (40, 64) or any(c not in '0123456789abcdef' for c in torrent_hash.lower()):
            raise ValueError('Invalid torrent hash')
        body = urllib.parse.urlencode({'hashes': torrent_hash, 'deleteFiles': 'true'}).encode()
        req = urllib.request.Request(self.base + '/api/v2/torrents/delete', data=body)
        with self.opener.open(req, timeout=15) as response:
            if response.status not in (200, 204):
                raise ValueError('Torrent deletion was not acknowledged')
        if self.get('torrents/info?' + urllib.parse.urlencode({'hashes': torrent_hash})):
            raise ValueError('Torrent still exists after deletion')

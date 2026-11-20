import os
import http.client
import re
import tempfile
import threading
import urllib.parse
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
from integrations import inspect_torrent_files
from integrations import Qbit
import app

class TorrentChecks(unittest.TestCase):
    def test_qbit_delete_uses_exact_hash_and_delete_files(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
        requests = []
        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                return Response()
        client = Qbit.__new__(Qbit)
        client.base = 'http://example.invalid'
        client.opener = Opener()
        client.get = lambda path: []
        client.delete_torrent('a' * 40)
        self.assertEqual(len(requests), 1)
        payload = urllib.parse.parse_qs(requests[0].data.decode())
        self.assertEqual(payload, {'hashes': ['a' * 40], 'deleteFiles': ['true']})
        with self.assertRaises(ValueError): client.delete_torrent('all')

    def test_batch_preview_skips_blocked_and_executes_only_confirmed(self):
        entry = {'plan': {'radarr_id': 1, 'title': 'Allowed'}, 'size': {'device': 1, 'inode': 2, 'size': 10, 'blocks': 1, 'links': 1}}
        def make_entry(mid):
            if mid == 2: raise ValueError('blocked')
            return entry
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        headers = {'Host': '127.0.0.1:8793', 'Origin': 'http://127.0.0.1:8793', 'Content-Type': 'application/x-www-form-urlencoded'}
        try:
            with patch.object(app, 'batch_entry', side_effect=make_entry), patch.object(app, 'radarr', return_value={'title': 'Blocked'}), patch.object(app, 'execute_deletion', return_value=(['Jellyfin', 'Radarr'], None)) as execute:
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
                conn.request('POST', '/batch-preview', urllib.parse.urlencode([('id', '1'), ('id', '2')]), headers)
                response = conn.getresponse(); page = response.read().decode()
                self.assertEqual(response.status, 200)
                self.assertIn('Allowed', page); self.assertIn('Blocked', page)
                self.assertEqual(execute.call_count, 0)
                token = re.search(r'name="signature" value="([a-f0-9]+)"', page).group(1)
                with patch.object(app, 'batch_entry', return_value={**entry, 'size': {**entry['size'], 'size': 99}}):
                    conn.request('POST', '/batch-delete', urllib.parse.urlencode({'id': '1', 'signature': token}), headers)
                    response = conn.getresponse(); response.read()
                    self.assertEqual(response.status, 409)
                    self.assertEqual(execute.call_count, 0)
                conn.request('POST', '/batch-delete', urllib.parse.urlencode({'id': '1', 'signature': token}), headers)
                response = conn.getresponse(); result = response.read().decode()
                self.assertEqual(response.status, 200)
                self.assertIn('Allowed', result)
                execute.assert_called_once_with(entry['plan'])
                conn.close()
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_batch_total_counts_hardlinks_once_when_all_links_selected(self):
        a = {'size': {'device': 1, 'inode': 10, 'size': 1000, 'blocks': 2, 'links': 2}}
        b = {'size': dict(a['size'])}
        self.assertEqual(app.batch_total([a]), (1000, 0))
        self.assertEqual(app.batch_total([a, b]), (2000, 1024))
        removing_torrent = {'size': {**a['size'], 'removed_links': 2}}
        self.assertEqual(app.batch_total([removing_torrent]), (1000, 1024))

    def test_batch_snapshot_changes_when_file_changes(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'file'; p.write_bytes(b'a')
            before = app.size_snapshot(p)
            p.write_bytes(b'longer')
            self.assertNotEqual(before, app.size_snapshot(p))

    def test_plan_rejects_multifile_torrent(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / 'film'; folder.mkdir(); video = folder / 'film.mkv'; video.write_bytes(b'fake')
            movie = {'path': '/data/film', 'movieFileId': 5, 'tmdbId': 22, 'title': 'Film'}
            related = {'jellyfin_id': 'abc', 'jellyfin_path': str(video), 'seerr_id': None}
            def fake_radarr(path):
                if path.startswith('moviefile/'): return {'path': '/data/film/film.mkv'}
                if path.startswith('history/'): return [{'eventType': 'downloadFolderImported', 'downloadId': 'abc123'}]
                return movie
            class FakeQbit:
                def __init__(self, *args): pass
                def get(self, path):
                    if path == 'torrents/info': return [{'hash': 'abc123', 'save_path': '/data'}]
                    return [{'name': 'film.mkv'}, {'name': 'other.mkv'}]
            with patch.object(app, 'MEDIA_ROOT', d), patch.object(app, 'MEDIA_UUID', 'uuid'), patch.object(app, 'mount_status', return_value=True), patch.object(app, 'related_records', return_value=related), patch.object(app, 'radarr', side_effect=fake_radarr), patch.object(app, 'Qbit', FakeQbit), patch.dict(os.environ, {'QBIT_URL': 'x', 'QBIT_USERNAME': 'x', 'QBIT_PASSWORD': 'x'}):
                with self.assertRaisesRegex(ValueError, 'multiple files'):
                    app.deletion_plan(1)

    def test_plan_accepts_unshared_single_file_hardlink(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / 'film'; folder.mkdir(); video = folder / 'film.mkv'; video.write_bytes(b'fake')
            download = Path(d) / 'downloads'; download.mkdir(); os.link(video, download / 'movie.mkv')
            movie = {'path': '/data/film', 'movieFileId': 5, 'tmdbId': 22, 'title': 'Film'}
            related = {'jellyfin_id': 'abc', 'jellyfin_path': str(video), 'seerr_id': None}
            def fake_radarr(path):
                if path.startswith('moviefile/'): return {'path': '/data/film/film.mkv'}
                if path.startswith('history/'): return [{'eventType': 'downloadFolderImported', 'downloadId': 'a' * 40}]
                return movie
            class FakeQbit:
                def __init__(self, *args): pass
                def get(self, path):
                    if path == 'torrents/info': return [{'hash': 'a' * 40, 'save_path': '/data/downloads'}]
                    return [{'name': 'movie.mkv'}]
            with patch.object(app, 'MEDIA_ROOT', d), patch.object(app, 'MEDIA_UUID', 'uuid'), patch.object(app, 'mount_status', return_value=True), patch.object(app, 'related_records', return_value=related), patch.object(app, 'radarr', side_effect=fake_radarr), patch.object(app, 'Qbit', FakeQbit), patch.dict(os.environ, {'QBIT_URL': 'x', 'QBIT_USERNAME': 'x', 'QBIT_PASSWORD': 'x'}):
                plan = app.deletion_plan(1)
                self.assertEqual(plan['torrent']['hash'], 'a' * 40)

    def test_delete_order_and_partial_failure(self):
        plan = {'jellyfin_id': 'abc', 'radarr_id': 42, 'seerr_id': 7}
        with patch.object(app, 'delete_request', side_effect=[None, RuntimeError('failed')]) as delete:
            done, error = app.execute_deletion(plan)
        self.assertEqual(done, ['Jellyfin'])
        self.assertIn('Radarr', error)
        self.assertEqual(delete.call_count, 2)
        with patch.object(app, 'delete_request') as delete:
            done, error = app.execute_deletion(plan)
        self.assertEqual(done, ['Jellyfin', 'Radarr', 'Seerr'])
        self.assertIsNone(error)
        self.assertEqual(delete.call_count, 3)

    def test_active_torrent_failure_stops_other_deletions(self):
        plan = {'torrent': {'hash': 'a' * 40}, 'jellyfin_id': 'abc', 'radarr_id': 42, 'seerr_id': 7}
        client = SimpleNamespace(delete_torrent=lambda _hash: (_ for _ in ()).throw(ValueError('failed')))
        with patch.object(app, 'Qbit', return_value=client), patch.object(app, 'delete_request') as delete, patch.dict(os.environ, {'QBIT_URL': 'x', 'QBIT_USERNAME': 'x', 'QBIT_PASSWORD': 'x'}):
            done, error = app.execute_deletion(plan)
        self.assertEqual(done, [])
        self.assertIn('qBittorrent', error)
        delete.assert_not_called()

    def test_active_torrent_removed_before_other_services(self):
        calls = []
        plan = {'torrent': {'hash': 'a' * 40, 'path': '/nonexistent/fake/file'}, 'jellyfin_id': 'abc', 'radarr_id': 42, 'seerr_id': 7}
        client = SimpleNamespace(delete_torrent=lambda value: calls.append(('torrent', value)))
        with patch.object(app, 'Qbit', return_value=client), patch.object(app, 'delete_request', side_effect=lambda url, headers: calls.append(('service', url))), patch.dict(os.environ, {'QBIT_URL': 'x', 'QBIT_USERNAME': 'x', 'QBIT_PASSWORD': 'x'}):
            done, error = app.execute_deletion(plan)
        self.assertEqual(done, ['qBittorrent', 'Jellyfin', 'Radarr', 'Seerr'])
        self.assertIsNone(error)
        self.assertEqual(calls[0][0], 'torrent')

    def test_remaining_torrent_file_stops_library_deletion(self):
        with tempfile.TemporaryDirectory() as d:
            remaining = Path(d) / 'video'; remaining.write_bytes(b'fake')
            plan = {'torrent': {'hash': 'a' * 40, 'path': str(remaining)}, 'jellyfin_id': 'abc', 'radarr_id': 42, 'seerr_id': 7}
            client = SimpleNamespace(delete_torrent=lambda value: None)
            with patch.object(app, 'Qbit', return_value=client), patch.object(app, 'delete_request') as delete, patch.dict(os.environ, {'QBIT_URL': 'x', 'QBIT_USERNAME': 'x', 'QBIT_PASSWORD': 'x'}):
                done, error = app.execute_deletion(plan)
            self.assertEqual(done, ['qBittorrent'])
            self.assertIn('qBittorrent', error)
            delete.assert_not_called()

    def test_related_records_rejects_duplicate_jellyfin_matches(self):
        with patch.object(app, 'jellyfin_movies', return_value=[{'ProviderIds': {'Tmdb': '123'}}, {'ProviderIds': {'Tmdb': '123'}}]):
            with self.assertRaises(ValueError):
                app.related_records({'tmdbId': 123})

    def test_matching_hardlink_and_packs(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'downloads'; root.mkdir()
            source = root / 'movie.mkv'; source.write_bytes(b'data')
            target = Path(d) / 'film.mkv'; os.link(source, target)
            check = lambda files: inspect_torrent_files(root, [{'name': x} for x in files], target)['safe']
            self.assertTrue(check(['movie.mkv', 'movie.srt']))
            self.assertFalse(check(['movie.mkv', 'other.mkv']))
            self.assertFalse(check(['../movie.mkv']))
            other = root / 'copy.mkv'; other.write_bytes(b'data')
            self.assertFalse(check(['copy.mkv']))

if __name__ == '__main__': unittest.main()

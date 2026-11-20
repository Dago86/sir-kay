#!/usr/bin/env python3
"""Local Jellyfin, Radarr, Seerr and qBittorrent movie cleanup."""
import html
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from integrations import Qbit, inspect_torrent_files, mount_status

RADARR = os.environ.get('RADARR_URL', 'http://127.0.0.1:7878').rstrip('/')
KEY = os.environ.get('RADARR_API_KEY', '')
JELLYFIN = os.environ.get('JELLYFIN_URL', '').rstrip('/')
JELLYFIN_TOKEN = os.environ.get('JELLYFIN_TOKEN', '')
JELLYFIN_USER_ID = os.environ.get('JELLYFIN_USER_ID', '')
SEERR_URL = os.environ.get('SEERR_URL', '').rstrip('/')
SEERR_API_KEY = os.environ.get('SEERR_API_KEY', '')
MEDIA_ROOT = os.environ.get('MEDIA_ROOT', '/srv/media')
MEDIA_UUID = os.environ.get('MEDIA_UUID', '')
SIGNING_KEY = secrets.token_bytes(32)
DELETE_LOCK = threading.Lock()


def host_path(path):
    if path == '/data':
        return MEDIA_ROOT
    if path.startswith('/data/') and '..' not in path.split('/'):
        return os.path.join(MEDIA_ROOT, path[6:])
    raise ValueError('Unmapped container path')


def torrent_preview(movie, hashes):
    if not MEDIA_UUID or not mount_status(MEDIA_ROOT, MEDIA_UUID):
        return 'Blocked: the media disk UUID and mount point do not match.'
    if not hashes:
        return 'No torrent identified in the import history; no automatic torrent action is available.'
    if len(hashes) != 1:
        return 'Ambiguous torrent association; manual review required.'
    q = Qbit(os.environ['QBIT_URL'], os.environ['QBIT_USERNAME'], os.environ['QBIT_PASSWORD'])
    torrents = q.get('torrents/info?' + urllib.parse.urlencode({'hashes': hashes[0]}))
    if not torrents:
        return 'The torrent in the import history is already absent from qBittorrent.'
    if len(torrents) != 1 or torrents[0]['hash'].lower() != hashes[0].lower():
        return 'Ambiguous torrent association; manual review required.'
    files = q.get('torrents/files?' + urllib.parse.urlencode({'hash': hashes[0]}))
    media = radarr('moviefile/' + str(movie['movieFileId']))
    result = inspect_torrent_files(host_path(torrents[0]['save_path']), files, host_path(media['path']))
    return result['reason'] + ' (preview only; ownership and all other safety checks remain required).'


def radarr(path):
    request = urllib.request.Request(RADARR + '/api/v3/' + path, headers={'X-Api-Key': KEY})
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.load(response)


def jellyfin_movies():
    if not JELLYFIN or not JELLYFIN_TOKEN or not JELLYFIN_USER_ID:
        raise ValueError('Jellyfin configuration missing')
    query = urllib.parse.urlencode({'Recursive': 'true', 'IncludeItemTypes': 'Movie', 'Fields': 'ProviderIds,Path', 'SortBy': 'SortName', 'SortOrder': 'Ascending'})
    headers = {'Authorization': f'MediaBrowser Client="Cascata", Device="Cascata", DeviceId="cascata-local", Version="1.0", Token="{JELLYFIN_TOKEN}"'}
    request = urllib.request.Request(JELLYFIN + '/Users/' + urllib.parse.quote(JELLYFIN_USER_ID, safe='') + '/Items?' + query, headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)['Items']


def related_records(movie):
    """Collect exact TMDB matches for the preview; never modify remote data."""
    tmdb = str(movie.get('tmdbId') or '')
    if not tmdb:
        raise ValueError('Missing TMDB ID')
    matches = [x for x in jellyfin_movies() if str((x.get('ProviderIds') or {}).get('Tmdb', '')) == tmdb]
    if len(matches) > 1:
        raise ValueError('Multiple Jellyfin items have the same TMDB ID')
    if not SEERR_URL or not SEERR_API_KEY:
        raise ValueError('Seerr is not configured')
    request = urllib.request.Request(SEERR_URL + '/api/v1/movie/' + urllib.parse.quote(tmdb, safe=''), headers={'X-Api-Key': SEERR_API_KEY})
    with urllib.request.urlopen(request, timeout=15) as response:
        seerr = json.load(response)
    info = seerr.get('mediaInfo') or {}
    if info and str(info.get('tmdbId')) != tmdb:
        raise ValueError('Seerr returned a mismatched TMDB ID')
    return {'jellyfin': matches[0].get('Name') if matches else None, 'jellyfin_id': matches[0].get('Id') if matches else None, 'jellyfin_path': matches[0].get('Path') if matches else None, 'seerr_id': info.get('id')}


def deletion_plan(movie_id):
    if not MEDIA_UUID or not mount_status(MEDIA_ROOT, MEDIA_UUID):
        raise ValueError('Media disk is not mounted correctly')
    movie = radarr('movie/' + str(movie_id))
    related = related_records(movie)
    if not related['jellyfin_id']:
        raise ValueError('Movie not found in Jellyfin')
    root = os.path.realpath(MEDIA_ROOT)
    target = os.path.realpath(host_path(movie['path']))
    jf_path = os.path.realpath(related['jellyfin_path'] or '')
    movie_file = radarr('moviefile/' + str(int(movie['movieFileId'])))
    radarr_file = os.path.realpath(host_path(movie_file['path']))
    if os.path.commonpath([root, target]) != root or target == root or os.path.commonpath([target, radarr_file]) != target or jf_path != radarr_file or not os.path.isfile(radarr_file):
        raise ValueError('Jellyfin and Radarr paths do not match')
    for directory, _, files in os.walk(target, followlinks=False):
        for filename in files:
            candidate = os.path.realpath(os.path.join(directory, filename))
            if filename.lower().endswith(('.mkv', '.mp4', '.avi', '.mov', '.m4v', '.ts', '.m2ts', '.wmv')) and candidate != radarr_file:
                raise ValueError('Movie folder contains other video files')
    history = radarr('history/movie?movieId=' + str(movie_id))
    hashes = sorted({e['downloadId'].lower() for e in history if e.get('eventType') == 'downloadFolderImported' and e.get('downloadId')})
    if not hashes:
        raise ValueError('No torrent identified in history; manual review required')
    q = Qbit(os.environ['QBIT_URL'], os.environ['QBIT_USERNAME'], os.environ['QBIT_PASSWORD'])
    torrents = q.get('torrents/info')
    live = [x for x in torrents if x['hash'].lower() in hashes]
    if len(live) > 1:
        raise ValueError('Multiple active torrents match this movie; manual review required')
    torrent_plan = None
    if live:
        torrent = live[0]
        torrent_hash = torrent['hash'].lower()
        files = q.get('torrents/files?' + urllib.parse.urlencode({'hash': torrent_hash}))
        if len(files) != 1:
            raise ValueError('Torrent contains multiple files; manual review required')
        save_path = host_path(torrent['save_path'])
        checked = inspect_torrent_files(save_path, files, radarr_file)
        if not checked['safe']:
            raise ValueError(checked['reason'])
        torrent_path = os.path.realpath(os.path.join(save_path, files[0]['name']))
        st = os.stat(torrent_path)
        if st.st_nlink != 2:
            raise ValueError('Torrent file has additional hard links; manual review required')
        for other in torrents:
            if other['hash'].lower() == torrent_hash:
                continue
            other_files = q.get('torrents/files?' + urllib.parse.urlencode({'hash': other['hash']}))
            other_root = host_path(other['save_path'])
            for item in other_files:
                candidate = os.path.realpath(os.path.join(other_root, item['name']))
                if candidate == torrent_path:
                    raise ValueError('Torrent data is shared with another torrent')
        torrent_plan = {'hash': torrent_hash, 'path': torrent_path, 'device': st.st_dev, 'inode': st.st_ino, 'size': st.st_size}
    return {'radarr_id': movie_id, 'tmdb': movie['tmdbId'], 'title': movie['title'], 'path': target, 'movie_file': radarr_file, 'jellyfin_id': related['jellyfin_id'], 'seerr_id': related['seerr_id'], 'hashes': hashes, 'torrent': torrent_plan}


def signature(plan):
    payload = json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()
    return hmac.new(SIGNING_KEY, payload, hashlib.sha256).hexdigest()


def delete_request(url, headers):
    req = urllib.request.Request(url, headers=headers, method='DELETE')
    with urllib.request.urlopen(req, timeout=15) as response:
        if response.status not in (200, 202, 204):
            raise ValueError('Deletion was not acknowledged')


def execute_deletion(plan):
    auth = {'Authorization': f'MediaBrowser Client="Cascata", Device="Cascata", DeviceId="cascata-local", Version="1.0", Token="{JELLYFIN_TOKEN}"'}
    completed = []
    if plan.get('torrent'):
        try:
            q = Qbit(os.environ['QBIT_URL'], os.environ['QBIT_USERNAME'], os.environ['QBIT_PASSWORD'])
            q.delete_torrent(plan['torrent']['hash'])
            completed.append('qBittorrent')
            if os.path.exists(plan['torrent']['path']):
                raise ValueError('Torrent entry removed but download file remains; verify manually')
        except Exception as exc:
            return completed, 'qBittorrent: ' + type(exc).__name__
    actions = [
        ('Jellyfin', JELLYFIN + '/Items/' + urllib.parse.quote(plan['jellyfin_id'], safe=''), auth),
        ('Radarr', RADARR + '/api/v3/movie/' + str(plan['radarr_id']) + '?deleteFiles=false&addImportExclusion=false', {'X-Api-Key': KEY}),
    ]
    if plan['seerr_id'] is not None:
        actions.append(('Seerr', SEERR_URL + '/api/v1/media/' + str(int(plan['seerr_id'])), {'X-Api-Key': SEERR_API_KEY}))
    for name, url, headers in actions:
        try:
            delete_request(url, headers)
            completed.append(name)
        except Exception as exc:
            return completed, name + ': ' + type(exc).__name__
    return completed, None


def esc(value):
    return html.escape(str(value or ''))


def file_size_description(movie):
    if not movie.get('movieFileId'):
        return 'File size unavailable.'
    entry = radarr('moviefile/' + str(int(movie['movieFileId'])))
    path = host_path(entry['path'])
    st = os.stat(path, follow_symlinks=False)
    size = f'{st.st_size / (1024 ** 3):.2f} GiB'
    if st.st_nlink > 1:
        return f'Movie size: {size}. There are {st.st_nlink} hard links to this file; space remains in use until all are removed.'
    return f'Movie size: {size}. If deletion succeeds, the file should free approximately {st.st_blocks * 512 / (1024 ** 3):.2f} GiB of disk space.'


def size_snapshot(path):
    st = os.stat(path, follow_symlinks=False)
    return {'device': st.st_dev, 'inode': st.st_ino, 'size': st.st_size, 'blocks': st.st_blocks, 'links': st.st_nlink}


def batch_total(entries):
    unique = {}
    for entry in entries:
        s = entry['size']
        key = (s['device'], s['inode'])
        if key not in unique:
            unique[key] = {'size': s, 'count': 0}
        unique[key]['count'] += s.get('removed_links', 1)
    return sum(e['size']['size'] for e in entries), sum(e['size']['blocks'] * 512 for e in unique.values() if e['count'] >= e['size']['links'])


def batch_entry(movie_id):
    plan = deletion_plan(movie_id)
    size = size_snapshot(plan['movie_file'])
    size['removed_links'] = 2 if plan.get('torrent') else 1
    return {'plan': plan, 'size': size}


class Handler(BaseHTTPRequestHandler):
    def page(self, body):
        data = ('<!doctype html><html lang="en"><meta charset="utf-8"><title>Cascata</title><body>' + body + '</body></html>').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def batch_action(self, form):
        raw = form.get('id', [])
        if not raw or len(raw) > 50:
            raise ValueError('Select between 1 and 50 films')
        ids = sorted({int(x) for x in raw})
        if len(ids) != len(raw) or any(x <= 0 for x in ids):
            raise ValueError('Invalid selection')
        if self.path == '/batch-preview':
            eligible, blocked = [], []
            for mid in ids:
                try:
                    eligible.append(batch_entry(mid))
                except (KeyError, ValueError, OSError, subprocess.SubprocessError) as exc:
                    blocked.append((mid, str(exc)))
            logical, freed = batch_total(eligible)
            body = '<dialog open><h1>Confirm deletion</h1><h2>Movies eligible for deletion</h2><ul>'
            for entry in eligible:
                body += '<li>' + esc(entry['plan']['title']) + ' — ' + f"{entry['size']['size'] / 1024**3:.2f}" + ' GiB — ' + ('torrent and data will be deleted' if entry['plan'].get('torrent') else 'torrent already absent') + '</li>'
            body += '</ul><p>Total movie size: ' + f'{logical / 1024**3:.2f}' + ' GiB<br>Estimated space freed: ' + f'{freed / 1024**3:.2f}' + ' GiB</p>'
            if blocked:
                body += '<h2>Blocked movies, which will be skipped</h2><ul>'
                for mid, reason in blocked:
                    try:
                        title = radarr('movie/' + str(mid)).get('title', str(mid))
                    except (OSError, ValueError, KeyError):
                        title = str(mid)
                    body += '<li>' + esc(title) + ': ' + esc(reason) + '</li>'
                body += '</ul>'
            if eligible:
                body += '<p>Movies will be deleted from Jellyfin and their Radarr and Seerr records removed. Listed eligible torrents and their files will also be deleted.</p><form action="/batch-delete" method="post">'
                for entry in eligible:
                    body += '<input type="hidden" name="id" value="' + str(entry['plan']['radarr_id']) + '">'
                body += '<input type="hidden" name="signature" value="' + signature(eligible) + '"><button type="submit">Confirm deletion</button></form>'
            body += '<p><a href="/">Cancel</a></p></dialog>'
            self.page(body)
            return
        received = form['signature'][0]
        with DELETE_LOCK:
            entries = [batch_entry(mid) for mid in ids]
            if not hmac.compare_digest(received, signature(entries)):
                raise ValueError('Preview expired')
            results = []
            for original in entries:
                try:
                    current = batch_entry(original['plan']['radarr_id'])
                    if current != original:
                        results.append((original['plan']['title'], 'Skipped: data changed after confirmation'))
                        continue
                    completed, error = execute_deletion(current['plan'])
                    result = 'Completed: ' + (', '.join(completed) or 'none')
                    if error:
                        result += '; stopped at ' + error
                    results.append((original['plan']['title'], result))
                except (KeyError, ValueError, OSError, subprocess.SubprocessError) as exc:
                    results.append((original['plan']['title'], 'Skipped: ' + str(exc)))
        body = '<h1>Results</h1><ul>' + ''.join('<li>' + esc(title) + ': ' + esc(result) + '</li>' for title, result in results) + '</ul><a href="/">Back to movies</a><script>sessionStorage.removeItem("cascata-selection")</script>'
        self.page(body)

    def do_GET(self):
        try:
            path = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(path.query)
            if path.path == '/':
                movies = jellyfin_movies()
                radarr_movies = radarr('movie')
                by_tmdb = {}
                for item in radarr_movies:
                    if item.get('tmdbId'):
                        by_tmdb.setdefault(str(item['tmdbId']), []).append(item)
                term = query.get('q', [''])[0].casefold().strip()
                matches = [m for m in movies if term in m.get('Name', '').casefold()]
                body = '<h1>Jellyfin movies</h1><p>Eligible torrents are removed together with their movies after confirmation.</p><form><input name="q" placeholder="Search movies" value="' + esc(term) + '"><button>Search</button> <a href="/">Show all</a></form><p>' + str(len(matches)) + ' movies</p><button type="button" onclick="selectVisible(true)">Select results</button> <button type="button" onclick="selectVisible(false)">Deselect results</button> <span id="count"></span><form id="batch" method="post" action="/batch-preview"><div id="selected"></div><button>Preview deletion</button></form><ul>'
                for m in matches:
                    label = esc(m.get('Name')) + (' (' + esc(m.get('ProductionYear')) + ')' if m.get('ProductionYear') else '')
                    candidates = by_tmdb.get(str((m.get('ProviderIds') or {}).get('Tmdb', '')), [])
                    if len(candidates) == 1:
                        candidate = candidates[0]
                        mid = str(int(candidate['id']))
                        try:
                            info = radarr('moviefile/' + str(int(candidate['movieFileId'])))
                            size = f"{os.stat(host_path(info['path']), follow_symlinks=False).st_size / 1024**3:.2f} GiB"
                        except (OSError, KeyError, ValueError):
                            size = 'unknown size'
                        body += '<li><input type="checkbox" class="pick" value="' + mid + '"> <a href="/movie?id=' + mid + '">' + label + '</a> — ' + esc(size) + '</li>'
                    else:
                        body += '<li>' + label + ' — Radarr match not verified</li>'
                body += '</ul>'
                body += '''<script>
const chosen = new Set(JSON.parse(sessionStorage.getItem('cascata-selection') || '[]'));
function render(){document.querySelectorAll('.pick').forEach(e=>e.checked=chosen.has(e.value));document.getElementById('count').textContent=chosen.size+' selected';document.getElementById('selected').replaceChildren(...Array.from(chosen, id=>{let e=document.createElement('input');e.type='hidden';e.name='id';e.value=id;return e}));sessionStorage.setItem('cascata-selection',JSON.stringify([...chosen]));}
document.querySelectorAll('.pick').forEach(e=>e.addEventListener('change',()=>{e.checked?chosen.add(e.value):chosen.delete(e.value);render()}));
function selectVisible(on){document.querySelectorAll('.pick').forEach(e=>on?chosen.add(e.value):chosen.delete(e.value));render()}
render();</script>'''
            elif path.path == '/movie':
                movie_id = int(query.get('id', [''])[0])
                movie = radarr('movie/' + str(movie_id))
                history = radarr('history/movie?movieId=' + str(movie_id))
                hashes = sorted({e['downloadId'] for e in history if e.get('eventType') == 'downloadFolderImported' and e.get('downloadId')})
                try:
                    detail = torrent_preview(movie, hashes)
                except (KeyError, ValueError, OSError, subprocess.SubprocessError):
                    detail = 'Blocked: missing configuration, inaccessible service, file or disk.'
                try:
                    related = related_records(movie)
                    other = 'Jellyfin: ' + (related['jellyfin'] or 'already absent') + '; Seerr: ' + ('present' if related['seerr_id'] else 'already absent')
                except (KeyError, ValueError, OSError):
                    other = 'Jellyfin or Seerr: verification failed.'
                try:
                    size_info = file_size_description(movie)
                except (KeyError, ValueError, OSError):
                    size_info = 'File size unavailable.'
                body = '<h1>' + esc(movie.get('title')) + '</h1><p>Path: ' + esc(movie.get('path')) + '</p><p>TMDB ID: ' + esc(movie.get('tmdbId')) + '</p><p>Import history torrent IDs: ' + str(len(hashes)) + '</p><p>' + esc(detail) + '</p><a href="/">Back</a>'
                body += '<p><strong>' + esc(size_info) + '</strong></p>'
                body += '<p>' + esc(other) + '</p>'
                try:
                    plan = deletion_plan(movie_id)
                    snap = size_snapshot(plan['movie_file'])
                    snap['removed_links'] = 2 if plan.get('torrent') else 1
                    _, freed = batch_total([{'size': snap}])
                    body += '<h2>Delete movie</h2><p>The movie will be deleted from Jellyfin and its Radarr record removed' + (' and its Seerr record' if plan['seerr_id'] is not None else '') + ('. The identified torrent and its file will also be deleted.' if plan.get('torrent') else '. The torrent is already absent from qBittorrent.') + '</p>'
                    body += '<p>Estimated disk space freed: ' + f'{freed / 1024**3:.2f}' + ' GiB.</p>'
                    body += '<form method="post" action="/delete"><input type="hidden" name="id" value="' + str(movie_id) + '"><input type="hidden" name="signature" value="' + signature(plan) + '"><button type="submit" onclick="return confirm(\'Permanently delete this movie and the listed records?\')">Delete permanently</button></form>'
                except (KeyError, ValueError, OSError, subprocess.SubprocessError) as exc:
                    body += '<p>Deletion unavailable: ' + esc(str(exc)) + '</p>'
            else:
                self.send_error(404); return
            data = ('<!doctype html><html lang="en"><meta charset="utf-8"><title>Movie cleanup preview</title><body>' + body + '</body></html>').encode()
            self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        except (ValueError, KeyError, OSError):
            self.send_error(503, 'Radarr is unavailable or the request is invalid')

    def do_POST(self):
        if self.path not in ('/delete', '/batch-preview', '/batch-delete') or self.headers.get('Host') != '127.0.0.1:8793' or self.headers.get('Origin') != 'http://127.0.0.1:8793':
            self.send_error(403); return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > 4096:
                raise ValueError('Invalid request length')
            form = urllib.parse.parse_qs(self.rfile.read(length).decode())
            if self.path in ('/batch-preview', '/batch-delete'):
                self.batch_action(form)
                return
            movie_id = int(form['id'][0])
            received = form['signature'][0]
            with DELETE_LOCK:
                plan = deletion_plan(movie_id)
                if not hmac.compare_digest(received, signature(plan)):
                    raise ValueError('Preview expired; reload the page')
                completed, error = execute_deletion(plan)
            result = 'Completed operations: ' + (', '.join(completed) if completed else 'none')
            if error:
                result += '. Operation stopped at ' + error + '; check the services before retrying.'
            else:
                result += '. Torrent cleanup completed or the torrent was already absent.'
            data = ('<!doctype html><meta charset="utf-8"><p>' + esc(result) + '</p><a href="/">Back to movies</a>').encode()
            self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        except (ValueError, KeyError, IndexError, OSError, subprocess.SubprocessError):
            self.send_error(409, 'Checks failed; no new deletion was started')


if __name__ == '__main__':
    if not KEY:
        raise SystemExit('Set RADARR_API_KEY before starting.')
    ThreadingHTTPServer(('127.0.0.1', 8793), Handler).serve_forever()

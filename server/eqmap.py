#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EQMap サーバー — 気象庁の震源データを SQLite に蓄積し、API と画面を配信する。

気象庁が公開している一覧は直近およそ30日分しかない。1日1回取り込んで
マージし続けることで、それより古い期間もアプリでたどれるようにする。

    eqmap.py update            1回だけ取得して DB を更新する（systemd timer / cron から呼ぶ）
    eqmap.py serve             HTTP サーバーを起動する
    eqmap.py stats             DB の状態を表示する
    eqmap.py import FILE.json  アプリの「書き出し」JSON を取り込む

Python 3.8+ の標準ライブラリのみで動く。pip インストールは不要。
"""

import argparse
import gzip
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

JMA_URL = 'https://www.jma.go.jp/bosai/quake/data/list.json'
USER_AGENT = 'EQMap/1.0 (+local archive; contact: local admin)'

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.environ.get('EQMAP_DB') or os.path.join(HERE, 'eqmap.db')
DEFAULT_WEB = os.environ.get('EQMAP_WEB') or os.path.join(HERE, 'web')

JST = timezone(timedelta(hours=9))

# 気象庁の一覧には遠地地震（インドネシア・南米など）も含まれる。
# 日本周辺だけを DB に入れる。
KEEP_BBOX = (120.0, 156.0, 20.0, 50.0)   # lon_min, lon_max, lat_min, lat_max

# 手動更新の最短間隔。気象庁側が Cache-Control: max-age=60 なので、
# それより短い間隔で叩いても意味がない。
MIN_FETCH_INTERVAL = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    eid        TEXT PRIMARY KEY,
    t          INTEGER NOT NULL,          -- 発生時刻 epoch ミリ秒
    lat        REAL    NOT NULL,
    lon        REAL    NOT NULL,
    depth      REAL,                      -- km。不明なら NULL
    mag        REAL,                      -- 不明なら NULL
    maxi       TEXT,                      -- 最大震度。不明なら NULL
    name       TEXT    NOT NULL DEFAULT '',
    ctt        TEXT    NOT NULL,          -- 採用した報の作成時刻
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_t ON events(t);
CREATE INDEX IF NOT EXISTS idx_events_mag ON events(mag);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      INTEGER NOT NULL,
    status  TEXT    NOT NULL,             -- ok | not_modified | error
    http    INTEGER,
    seen    INTEGER,
    added   INTEGER,
    updated INTEGER,
    detail  TEXT
);
"""


# ---------------------------------------------------------------- DB

def connect(path):
    first = not os.path.exists(path)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.executescript(SCHEMA)
    conn.commit()
    if first:
        log('新しい DB を作成しました: %s' % path)
    return conn


def get_meta(conn, key, default=None):
    row = conn.execute('SELECT v FROM meta WHERE k=?', (key,)).fetchone()
    return row['v'] if row else default


def set_meta(conn, key, value):
    conn.execute('INSERT INTO meta(k,v) VALUES(?,?) '
                 'ON CONFLICT(k) DO UPDATE SET v=excluded.v', (key, str(value)))


def log(msg):
    stamp = datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')
    print('[%s] %s' % (stamp, msg), flush=True)


# ---------------------------------------------------- 気象庁データの正規化

COD_RE = re.compile(r'^([+-][\d.]+)([+-][\d.]+)(?:([+-][\d.]+))?/')


def to_degrees(value, limit):
    """ISO 6709 風の座標を度に直す。

    通常は十進度（+32.5）だが、稀に度分（+3237.5 = 32°37.5'）が混ざる。
    値域を超えていたら度分とみなして換算する。
    """
    if value != value or value in (float('inf'), float('-inf')):
        return None
    if abs(value) <= limit:
        return value
    sign = -1.0 if value < 0 else 1.0
    a = abs(value)
    deg = int(a // 100)
    minutes = a - deg * 100
    d = sign * (deg + minutes / 60.0)
    return d if abs(d) <= limit else None


def parse_cod(cod):
    """"+32.5+130.5-10000/" -> (lat, lon, depth_km)。深さは欠けることがある。"""
    m = COD_RE.match(cod or '')
    if not m:
        return None
    try:
        lat = to_degrees(float(m.group(1)), 90.0)
        lon = to_degrees(float(m.group(2)), 180.0)
    except ValueError:
        return None
    if lat is None or lon is None:
        return None
    depth = None
    if m.group(3) is not None:
        try:
            depth = -float(m.group(3)) / 1000.0
        except ValueError:
            depth = None
    return lat, lon, depth


def parse_at(value):
    """"2026-08-21T03:23:00+09:00" -> epoch ミリ秒。"""
    try:
        return int(datetime.fromisoformat(value).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def normalize(raw):
    """一覧の生レコード列 -> DB 行の列。

    同じ地震（eid）に複数の報が来るので、震源座標を持つ報のうち
    ctt（作成時刻）が最大のものを採る。震度速報は cod が空なのでここで落ちる。
    """
    best = {}
    for r in raw:
        if not isinstance(r, dict):
            continue
        pos = parse_cod(r.get('cod'))
        if not pos:
            continue
        t = parse_at(r.get('at'))
        if t is None:
            continue
        lat, lon, depth = pos
        lon_min, lon_max, lat_min, lat_max = KEEP_BBOX
        if not (lon_min <= lon <= lon_max and lat_min <= lat <= lat_max):
            continue

        eid = str(r.get('eid') or r.get('at'))
        ctt = str(r.get('ctt') or '')
        prev = best.get(eid)
        if prev and prev['ctt'] >= ctt:
            continue

        mag = r.get('mag')
        try:
            mag = float(mag) if mag not in ('', None) else None
        except (TypeError, ValueError):
            mag = None
        maxi = r.get('maxi')
        maxi = str(maxi) if maxi not in ('', None) else None

        best[eid] = {
            'eid': eid,
            't': t,
            'lat': round(lat, 4),
            'lon': round(lon, 4),
            'depth': None if depth is None else round(depth, 1),
            'mag': mag,
            'maxi': maxi,
            'name': str(r.get('anm') or ''),
            'ctt': ctt,
        }
    return list(best.values())


def upsert(conn, rows):
    """(新規件数, 更新件数) を返す。報が古いものは無視する。"""
    now = int(time.time() * 1000)
    added = updated = 0
    for row in rows:
        cur = conn.execute('SELECT ctt FROM events WHERE eid=?', (row['eid'],)).fetchone()
        if cur is None:
            conn.execute(
                'INSERT INTO events(eid,t,lat,lon,depth,mag,maxi,name,ctt,updated_at) '
                'VALUES(:eid,:t,:lat,:lon,:depth,:mag,:maxi,:name,:ctt,:now)',
                dict(row, now=now))
            added += 1
        elif row['ctt'] > cur['ctt']:
            conn.execute(
                'UPDATE events SET t=:t,lat=:lat,lon=:lon,depth=:depth,mag=:mag,'
                'maxi=:maxi,name=:name,ctt=:ctt,updated_at=:now WHERE eid=:eid',
                dict(row, now=now))
            updated += 1
    conn.commit()
    return added, updated


# ---------------------------------------------------------------- 取得

_fetch_lock = threading.Lock()


def do_update(conn, force=False):
    """気象庁から取得して DB を更新する。結果の dict を返す。

    ETag / Last-Modified を保存しておき、条件付き GET で問い合わせる。
    変更がなければ 304 が返り、本文は転送されない。
    """
    with _fetch_lock:
        started = int(time.time() * 1000)
        req = urllib.request.Request(JMA_URL, headers={
            'User-Agent': USER_AGENT,
            'Accept': 'application/json',
            'Accept-Encoding': 'gzip',
        })
        if not force:
            etag = get_meta(conn, 'etag')
            lastmod = get_meta(conn, 'last_modified')
            if etag:
                req.add_header('If-None-Match', etag)
            if lastmod:
                req.add_header('If-Modified-Since', lastmod)

        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                payload = res.read()
                if res.headers.get('Content-Encoding') == 'gzip':
                    payload = gzip.decompress(payload)
                etag = res.headers.get('ETag')
                lastmod = res.headers.get('Last-Modified')
                code = res.status
        except urllib.error.HTTPError as err:
            if err.code == 304:
                set_meta(conn, 'last_fetch', started)
                set_meta(conn, 'last_status', 'not_modified')
                conn.execute('INSERT INTO fetch_log(at,status,http,seen,added,updated,detail) '
                             'VALUES(?,?,?,?,?,?,?)',
                             (started, 'not_modified', 304, None, 0, 0, None))
                conn.commit()
                log('更新なし (304)')
                return {'status': 'not_modified', 'http': 304,
                        'seen': 0, 'added': 0, 'updated': 0}
            return _record_error(conn, started, 'HTTP %s %s' % (err.code, err.reason), err.code)
        except Exception as err:                                  # noqa: BLE001
            return _record_error(conn, started, '%s: %s' % (type(err).__name__, err), None)

        try:
            raw = json.loads(payload.decode('utf-8'))
            if not isinstance(raw, list):
                raise ValueError('予期しない応答形式（配列ではない）')
        except Exception as err:                                  # noqa: BLE001
            return _record_error(conn, started, '解析に失敗: %s' % err, code)

        rows = normalize(raw)
        added, updated = upsert(conn, rows)

        if etag:
            set_meta(conn, 'etag', etag)
        if lastmod:
            set_meta(conn, 'last_modified', lastmod)
        set_meta(conn, 'last_fetch', started)
        set_meta(conn, 'last_status', 'ok')
        set_meta(conn, 'last_added', added)
        conn.execute('INSERT INTO fetch_log(at,status,http,seen,added,updated,detail) '
                     'VALUES(?,?,?,?,?,?,?)',
                     (started, 'ok', code, len(rows), added, updated, None))
        conn.commit()
        log('取得成功: 一覧 %d 件 / 新規 %d 件 / 更新 %d 件 (合計 %d 件)'
            % (len(rows), added, updated, count_events(conn)))
        return {'status': 'ok', 'http': code, 'seen': len(rows),
                'added': added, 'updated': updated}


def _record_error(conn, started, detail, code):
    set_meta(conn, 'last_fetch', started)
    set_meta(conn, 'last_status', 'error')
    conn.execute('INSERT INTO fetch_log(at,status,http,seen,added,updated,detail) '
                 'VALUES(?,?,?,?,?,?,?)', (started, 'error', code, None, 0, 0, detail))
    conn.commit()
    log('取得失敗: %s' % detail)
    return {'status': 'error', 'http': code, 'detail': detail,
            'seen': 0, 'added': 0, 'updated': 0}


# ---------------------------------------------------------------- 問い合わせ

def count_events(conn):
    return conn.execute('SELECT COUNT(*) AS n FROM events').fetchone()['n']


def query_events(conn, t_from=None, t_to=None, min_mag=None, limit=200000):
    where, args = [], []
    if t_from is not None:
        where.append('t >= ?'); args.append(int(t_from))
    if t_to is not None:
        where.append('t <= ?'); args.append(int(t_to))
    if min_mag is not None:
        where.append('mag IS NOT NULL AND mag >= ?'); args.append(float(min_mag))
    clause = (' WHERE ' + ' AND '.join(where)) if where else ''
    # limit+1 件取って、超えていたら打ち切られたことを呼び出し側へ伝える
    sql = ('SELECT eid,t,lat,lon,depth,mag,maxi,name FROM events'
           + clause + ' ORDER BY t ASC LIMIT ?')
    args.append(int(limit) + 1)
    rows = conn.execute(sql, args).fetchall()
    truncated = len(rows) > limit
    if truncated:
        rows = rows[:limit]
    return [[r['eid'], r['t'], r['lat'], r['lon'], r['depth'],
             r['mag'], r['maxi'], r['name']] for r in rows], truncated


def status_dict(conn, db_path):
    row = conn.execute('SELECT COUNT(*) AS n, MIN(t) AS lo, MAX(t) AS hi FROM events').fetchone()
    recent = conn.execute('SELECT at,status,http,seen,added,updated,detail FROM fetch_log '
                          'ORDER BY id DESC LIMIT 10').fetchall()
    try:
        db_bytes = os.path.getsize(db_path)
    except OSError:
        db_bytes = None
    return {
        'count': row['n'],
        'tMin': row['lo'],
        'tMax': row['hi'],
        'lastFetch': int(get_meta(conn, 'last_fetch') or 0) or None,
        'lastStatus': get_meta(conn, 'last_status'),
        'lastAdded': int(get_meta(conn, 'last_added') or 0),
        'dbBytes': db_bytes,
        'serverTime': int(time.time() * 1000),
        'source': JMA_URL,
        'recent': [dict(r) for r in recent],
    }


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = 'EQMap'
    protocol_version = 'HTTP/1.1'

    # --- 応答ヘルパ ---

    def _send(self, code, body, ctype, extra=None, allow_gzip=True):
        if isinstance(body, str):
            body = body.encode('utf-8')
        headers = {'Content-Type': ctype}
        if extra:
            headers.update(extra)
        accepts_gzip = 'gzip' in (self.headers.get('Accept-Encoding') or '')
        if allow_gzip and accepts_gzip and len(body) > 1024:
            body = gzip.compress(body, 6)
            headers['Content-Encoding'] = 'gzip'
            headers['Vary'] = 'Accept-Encoding'
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, code, obj, extra=None):
        head = {'Cache-Control': 'no-store'}
        if self.server.cors:
            head['Access-Control-Allow-Origin'] = '*'
        if extra:
            head.update(extra)
        self._send(code, json.dumps(obj, ensure_ascii=False), 'application/json; charset=utf-8', head)

    def _error(self, code, message):
        self._json(code, {'error': message})

    # --- ルーティング ---

    def do_OPTIONS(self):
        head = {'Allow': 'GET, POST, HEAD, OPTIONS'}
        if self.server.cors:
            head.update({'Access-Control-Allow-Origin': '*',
                         'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
                         'Access-Control-Allow-Headers': 'Content-Type'})
        self._send(204, b'', 'text/plain', head, allow_gzip=False)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        path, query = parts.path, urllib.parse.parse_qs(parts.query)
        try:
            if path == '/api/events':
                return self._api_events(query)
            if path == '/api/status':
                return self._json(200, status_dict(self.server.db(), self.server.db_path))
            if path.startswith('/api/'):
                return self._error(404, 'unknown endpoint')
            return self._static(path)
        except BrokenPipeError:
            pass
        except Exception as err:                                  # noqa: BLE001
            log('リクエスト処理でエラー (%s): %r' % (path, err))
            try:
                self._error(500, str(err))
            except Exception:                                     # noqa: BLE001
                pass

    def do_POST(self):
        parts = urllib.parse.urlsplit(self.path)
        if parts.path != '/api/refresh':
            return self._error(404, 'unknown endpoint')
        # 気象庁を叩きすぎないよう、最低間隔を空ける
        conn = self.server.db()
        last = int(get_meta(conn, 'last_fetch') or 0)
        wait = MIN_FETCH_INTERVAL - (time.time() - last / 1000.0)
        if last and wait > 0:
            return self._json(429, {'status': 'throttled', 'retryAfter': int(wait) + 1,
                                    'detail': '前回の取得から %d 秒しか経っていません' % (MIN_FETCH_INTERVAL - int(wait))},
                              {'Retry-After': str(int(wait) + 1)})
        result = do_update(conn)
        result['count'] = count_events(conn)
        code = 200 if result['status'] != 'error' else 502
        return self._json(code, result)

    def _api_events(self, query):
        def num(name, cast):
            raw = query.get(name, [None])[0]
            if raw in (None, ''):
                return None
            try:
                return cast(raw)
            except ValueError:
                raise ValueError('%s の値が不正です: %r' % (name, raw))

        try:
            t_from, t_to = num('from', int), num('to', int)
            min_mag = num('min_mag', float)
            limit = num('limit', int) or 200000
        except ValueError as err:
            return self._error(400, str(err))
        limit = max(1, min(limit, 500000))

        conn = self.server.db()
        events, truncated = query_events(conn, t_from, t_to, min_mag, limit)
        self._json(200, {
            'events': events,
            'count': len(events),
            'truncated': truncated,
            'total': count_events(conn),
            'serverTime': int(time.time() * 1000),
            'lastFetch': int(get_meta(conn, 'last_fetch') or 0) or None,
            'lastStatus': get_meta(conn, 'last_status'),
            'fields': ['eid', 't', 'lat', 'lon', 'depth', 'mag', 'maxi', 'name'],
        })

    TYPES = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
             '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
             '.png': 'image/png', '.svg': 'image/svg+xml', '.ico': 'image/x-icon',
             '.txt': 'text/plain; charset=utf-8', '.md': 'text/markdown; charset=utf-8'}

    def _static(self, path):
        root = self.server.web_root
        rel = path.lstrip('/') or 'index.html'
        target = os.path.normpath(os.path.join(root, rel))
        # ディレクトリ外への脱出を防ぐ
        if not (target == root or target.startswith(root + os.sep)):
            return self._error(403, 'forbidden')
        if os.path.isdir(target):
            target = os.path.join(target, 'index.html')
        if not os.path.isfile(target):
            return self._error(404, 'not found')
        with open(target, 'rb') as f:
            body = f.read()
        ctype = self.TYPES.get(os.path.splitext(target)[1].lower(), 'application/octet-stream')
        self._send(200, body, ctype, {'Cache-Control': 'no-cache'})

    def log_message(self, fmt, *args):
        if self.server.verbose:
            log('%s %s' % (self.address_string(), fmt % args))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, db_path, web_root, cors, verbose):
        super().__init__(addr, Handler)
        self.db_path = db_path
        self.web_root = os.path.normpath(web_root)
        self.cors = cors
        self.verbose = verbose
        self._local = threading.local()

    def db(self):
        """スレッドごとに接続を持つ（sqlite3 の接続はスレッドをまたげない）。"""
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = connect(self.db_path)
            self._local.conn = conn
        return conn


# ---------------------------------------------------------------- CLI

def cmd_update(args):
    conn = connect(args.db)
    result = do_update(conn, force=args.force)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] != 'error' else 1


def cmd_serve(args):
    conn = connect(args.db)
    n = count_events(conn)
    conn.close()
    if not os.path.isdir(args.web):
        log('警告: web ディレクトリがありません: %s' % args.web)
    srv = Server((args.host, args.port), args.db, args.web, args.cors, args.verbose)
    log('起動しました  http://%s:%d/  (DB %d 件, %s)'
        % (args.host if args.host != '0.0.0.0' else '0.0.0.0', args.port, n, args.db))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log('停止します')
    finally:
        srv.server_close()
    return 0


def cmd_stats(args):
    conn = connect(args.db)
    st = status_dict(conn, args.db)
    fmt = lambda ms: datetime.fromtimestamp(ms / 1000, JST).strftime('%Y-%m-%d %H:%M') if ms else '—'
    print('DB           : %s' % args.db)
    print('件数         : %s' % format(st['count'], ','))
    print('期間         : %s 〜 %s' % (fmt(st['tMin']), fmt(st['tMax'])))
    print('最終取得     : %s (%s)' % (fmt(st['lastFetch']), st['lastStatus'] or '—'))
    print('DB サイズ    : %.1f MB' % ((st['dbBytes'] or 0) / 1e6))
    print('\n直近の取得ログ:')
    for r in st['recent']:
        print('  %s  %-13s http=%-4s 一覧=%-5s 新規=%-4s 更新=%-4s %s'
              % (fmt(r['at']), r['status'], r['http'] if r['http'] else '—',
                 r['seen'] if r['seen'] is not None else '—',
                 r['added'], r['updated'], r['detail'] or ''))
    return 0


def cmd_import(args):
    """アプリの「書き出し」JSON（配列の配列）を取り込む。"""
    with open(args.file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, list):
        print('配列ではありません', file=sys.stderr)
        return 1
    rows, skipped = [], 0
    for e in data:
        if not isinstance(e, list) or len(e) < 8:
            skipped += 1
            continue
        eid, t, lat, lon, depth, mag, maxi, name = e[:8]
        lon_min, lon_max, lat_min, lat_max = KEEP_BBOX
        if not (lon_min <= lon <= lon_max and lat_min <= lat <= lat_max):
            skipped += 1
            continue
        rows.append({'eid': str(eid), 't': int(t), 'lat': float(lat), 'lon': float(lon),
                     'depth': None if depth is None else float(depth),
                     'mag': None if mag is None else float(mag),
                     'maxi': None if maxi in (None, '') else str(maxi),
                     'name': str(name or ''),
                     # 取り込みは既存の報を上書きしない（気象庁由来の ctt の方が信頼できる）
                     'ctt': '0'})
    conn = connect(args.db)
    added, updated = upsert(conn, rows)
    print('取り込み: 新規 %d 件 / 更新 %d 件 / 無視 %d 件 (合計 %d 件)'
          % (added, updated, skipped, count_events(conn)))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', default=DEFAULT_DB, help='SQLite ファイル (既定: %(default)s)')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('update', help='1回だけ取得して DB を更新する')
    p.add_argument('--force', action='store_true',
                   help='ETag を無視して必ず本文を取得する')
    p.set_defaults(func=cmd_update)

    p = sub.add_parser('serve', help='HTTP サーバーを起動する')
    p.add_argument('--host', default='0.0.0.0')
    p.add_argument('--port', type=int, default=8787)
    p.add_argument('--web', default=DEFAULT_WEB, help='静的ファイルの置き場 (既定: %(default)s)')
    p.add_argument('--cors', action='store_true',
                   help='API に Access-Control-Allow-Origin: * を付ける（別オリジンから使う場合）')
    p.add_argument('--verbose', action='store_true', help='アクセスログを出す')
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser('stats', help='DB の状態を表示する')
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser('import', help='アプリの書き出し JSON を取り込む')
    p.add_argument('file')
    p.set_defaults(func=cmd_import)

    # 端末のロケールが UTF-8 でなくても落ちないようにする（Windows の cp932 など）
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())

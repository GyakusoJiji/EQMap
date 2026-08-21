#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tenki.jp 地震履歴スクレイパー

https://earthquake.tenki.jp/bousai/earthquake/entries/ から過去全件の地震情報を取得し、
EQMap の SQLite データベース (eqmap.db) および JSON アライメントデータへ保存・マージする。
"""

import os
import re
import sys
import time
import json
import sqlite3
import urllib.request
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)

JST = timezone(timedelta(hours=9))

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, '..'))
DB_PATH = os.environ.get('EQMAP_DB') or os.path.join(PROJECT_ROOT, 'server', 'eqmap.db')
CACHE_FILE = os.path.join(HERE, 'tenki_cache.jsonl')
EXPORT_JSON = os.path.join(PROJECT_ROOT, 'eqmap-archive-tenki.json')

BASE_URL = "https://earthquake.tenki.jp"
ENTRIES_URL_TMPL = "https://earthquake.tenki.jp/bousai/earthquake/entries/page-{page}.html"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
}

# ---------------------------------------------------- DB Setup
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
    ctt        TEXT    NOT NULL,          -- 作成時刻 / 出典
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_t ON events(t);
CREATE INDEX IF NOT EXISTS idx_events_mag ON events(mag);
"""

def init_db(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.executescript(SCHEMA)
    conn.commit()
    return conn

# ---------------------------------------------------- Fetch Helper
def fetch_url(url, retries=3, timeout=10):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as res:
                raw = res.read()
                return raw.decode('utf-8', errors='ignore')
        except Exception as e:
            if i == retries - 1:
                return None
            time.sleep(0.3 * (i + 1))

# ---------------------------------------------------- Page Parsers
def parse_entries_page(html):
    """一覧ページから詳細ページの相対URLリストを取得"""
    matches = re.findall(r'href="(/bousai/earthquake/detail/.*?\.html)"', html)
    seen = set()
    urls = []
    for u in matches:
        if u not in seen:
            seen.add(u)
            urls.append(u)
    return urls

def parse_detail_page(url, html):
    """詳細ページから地震データを抽出"""
    if not html:
        return None

    m_id = re.search(r'(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})\.html', url)
    if not m_id:
        return None

    y, m, d, hh, mm, ss = m_id.groups()
    eid = f"{y}{m}{d}{hh}{mm}{ss}"

    # 1. 発生時刻
    m_t = re.search(r'(\d{4})年(\d{2})月(\d{2})日\s*(\d{2})時(\d{2})分', html)
    if m_t:
        dt = datetime(int(m_t.group(1)), int(m_t.group(2)), int(m_t.group(3)),
                      int(m_t.group(4)), int(m_t.group(5)), int(ss), tzinfo=JST)
        t_epoch_ms = int(dt.timestamp() * 1000)
    else:
        dt = datetime(int(y), int(m), int(d), int(hh), int(mm), int(ss), tzinfo=JST)
        t_epoch_ms = int(dt.timestamp() * 1000)

    # 2. 緯度・経度
    m_lat = re.search(r'([北南]緯)\s*([\d\.]+)度', html)
    m_lon = re.search(r'([東西]経)\s*([\d\.]+)度', html)
    if not m_lat or not m_lon:
        return None

    lat = float(m_lat.group(2))
    if '南緯' in m_lat.group(1):
        lat = -lat

    lon = float(m_lon.group(2))
    if '西経' in m_lon.group(1):
        lon = -lon

    # 3. 震源地名
    m_name = re.search(r'<th.*?>\s*震源地\s*</th>\s*<td.*?>(.*?)</td>', html, re.DOTALL)
    name = re.sub(r'<.*?>', '', m_name.group(1)).strip() if m_name else '不明'
    name = re.sub(r'\s+', ' ', name)

    # 4. 深さ
    m_dep = re.search(r'深さ\s*</th>\s*<td.*?>(.*?)</td>', html, re.DOTALL)
    dep_text = re.sub(r'<.*?>', '', m_dep.group(1)).strip() if m_dep else ''
    if 'ごく浅い' in dep_text:
        depth = 0.0
    else:
        m_dep_val = re.search(r'([\d\.]+)\s*km', dep_text)
        depth = float(m_dep_val.group(1)) if m_dep_val else None

    # 5. マグニチュード
    m_mag = re.search(r'マグニチュード\s*</th>\s*<td.*?>(.*?)</td>', html, re.DOTALL)
    mag_text = re.sub(r'<.*?>', '', m_mag.group(1)).strip() if m_mag else ''
    m_mag_val = re.search(r'M([\d\.]+)', mag_text)
    if not m_mag_val:
        m_mag_val = re.search(r'M([\d\.]+)', html)
    mag = float(m_mag_val.group(1)) if m_mag_val else None

    # 6. 最大震度
    m_maxi = re.search(r'最大震度\s*</th>\s*<td.*?>(.*?)</td>', html, re.DOTALL)
    maxi_text = re.sub(r'<.*?>', '', m_maxi.group(1)).strip() if m_maxi else ''
    maxi_map = {
        '震度1': '1', '震度2': '2', '震度3': '3', '震度4': '4',
        '震度5弱': '5-', '震度5強': '5+', '震度6弱': '6-', '震度6強': '6+', '震度7': '7'
    }
    maxi = None
    for k, v in maxi_map.items():
        if k in maxi_text:
            maxi = v
            break

    ctt_str = f"{y}-{m}-{d}T{hh}:{mm}:{ss}+09:00"

    return {
        'eid': eid,
        't': t_epoch_ms,
        'lat': lat,
        'lon': lon,
        'depth': depth,
        'mag': mag,
        'maxi': maxi,
        'name': name,
        'ctt': ctt_str,
        'url': url
    }

# ---------------------------------------------------- Main Scraping Logic
def run_scraper(max_pages=450, max_workers=5):
    print(f"=== tenki.jp 地震データスクレイピング開始 (最大 {max_pages} ページ, 並列スレッド: {max_workers}) ===")
    
    cached_data = {}
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    try:
                        item = json.loads(line)
                        cached_data[item['eid']] = item
                    except Exception:
                        pass
        print(f"キャッシュから {len(cached_data)} 件の既存データを読み込みました。")

    # Step 1: 全一覧ページから詳細 URL を収集
    print("Step 1: 一覧ページをスキャンして詳細ページ URL を収集しています...")
    detail_urls = []
    
    def process_entries_page(p):
        url = "https://earthquake.tenki.jp/bousai/earthquake/entries/" if p == 1 else ENTRIES_URL_TMPL.format(page=p)
        html = fetch_url(url)
        if html:
            urls = parse_entries_page(html)
            return p, urls
        return p, []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_entries_page, p) for p in range(1, max_pages + 1)]
        for future in as_completed(futures):
            p, urls = future.result()
            if urls:
                detail_urls.extend(urls)
                if p % 50 == 0 or p == 1 or p == max_pages:
                    print(f"  [一覧] Page {p}/{max_pages} 取得完了 (現在合計 {len(detail_urls)} URL)")

    unique_urls = list(dict.fromkeys(detail_urls))
    print(f"Step 1 完了: 合計 {len(unique_urls)} 件の詳細 URL を特定しました。")

    urls_to_fetch = []
    for u in unique_urls:
        m = re.search(r'(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})\.html', u)
        if m:
            eid = "".join(m.groups())
            if eid not in cached_data:
                urls_to_fetch.append(u)

    print(f"Step 2: 未取得の {len(urls_to_fetch)} 件の詳細ページを収集します...")

    cache_fd = open(CACHE_FILE, 'a', encoding='utf-8')

    processed = 0
    start_time = time.time()

    def process_detail_url(u):
        full_url = BASE_URL + u if u.startswith('/') else u
        html = fetch_url(full_url, retries=2, timeout=8)
        item = parse_detail_page(u, html)
        return item

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_detail_url, u): u for u in urls_to_fetch}
        for future in as_completed(futures):
            processed += 1
            item = future.result()
            if item:
                cached_data[item['eid']] = item
                cache_fd.write(json.dumps(item, ensure_ascii=False) + '\n')
                cache_fd.flush()

            if processed % 100 == 0 or processed == len(urls_to_fetch):
                elapsed = time.time() - start_time
                rate = processed / elapsed if elapsed > 0 else 0
                remaining = (len(urls_to_fetch) - processed) / rate if rate > 0 else 0
                print(f"  [進捗] {processed}/{len(urls_to_fetch)} ({processed/len(urls_to_fetch)*100:.1f}%) "
                      f"- 取得済み総計: {len(cached_data)}件 | 速度: {rate:.1f}件/秒 | 残り時間想定: {remaining/60:.1f}分")

    cache_fd.close()
    print(f"Step 2 完了: 合計 {len(cached_data)} 件の地震データを収集・キャッシュしました。")

    # Step 3: SQLite DB へのマージ
    print(f"Step 3: SQLite DB ({DB_PATH}) にマージしています...")
    conn = init_db(DB_PATH)
    now_ms = int(time.time() * 1000)

    db_items = []
    export_array = []

    for item in cached_data.values():
        eid = item['eid']
        t = item['t']
        lat = item['lat']
        lon = item['lon']
        depth = item['depth']
        mag = item['mag']
        maxi = item['maxi']
        name = item['name']
        ctt = item['ctt']

        db_items.append((eid, t, lat, lon, depth, mag, maxi, name, ctt, now_ms))
        export_array.append([eid, t, lat, lon, depth, mag, maxi, name])

    conn.executemany("""
        INSERT INTO events (eid, t, lat, lon, depth, mag, maxi, name, ctt, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(eid) DO UPDATE SET
            t=excluded.t, lat=excluded.lat, lon=excluded.lon,
            depth=excluded.depth, mag=excluded.mag, maxi=excluded.maxi,
            name=excluded.name, ctt=excluded.ctt, updated_at=excluded.updated_at
    """, db_items)
    conn.commit()
    conn.close()
    print(f"SQLite DB へのマージ完了 (総件数: {len(db_items)})")

    # Step 4: 単体モード用 JSON の保存
    print(f"Step 4: 単体モード用 JSON ({EXPORT_JSON}) を出力しています...")
    export_array.sort(key=lambda x: x[1])
    with open(EXPORT_JSON, 'w', encoding='utf-8') as f:
        json.dump(export_array, f, ensure_ascii=False, separators=(',', ':'))

    print(f"=== すべての処理が完了しました！ (合計: {len(export_array)} 件) ===")

if __name__ == '__main__':
    max_p = 450
    if len(sys.argv) > 1:
        max_p = int(sys.argv[1])
    run_scraper(max_pages=max_p, max_workers=6)

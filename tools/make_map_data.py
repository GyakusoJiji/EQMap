#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EQMap 同梱用の日本地図データを生成する。

Natural Earth (Public Domain) の admin_0 countries から日本のポリゴンを抜き出し、
Douglas-Peucker で簡略化して index.html に貼り付けられる JS スニペットを吐く。

    python tools/make_map_data.py            # 既定 tolerance で生成
    python tools/make_map_data.py --tol 0.02 # もっと粗く（ファイルを小さく）

出力: tools/japan.geo.js   ->  const JAPAN_GEO = [[[lon,lat],...], ...];

標準ライブラリのみ。pip 依存なし。
"""

import argparse
import json
import os
import sys
import urllib.request

SOURCE_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
    "geojson/ne_10m_admin_0_countries.geojson"
)
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(HERE, "_ne_10m_admin_0_countries.geojson")
OUT_PATH = os.path.join(HERE, "japan.geo.js")


def download(url, cache_path):
    """元データを取得する。一度落としたらキャッシュを使い回す（13MB あるので）。"""
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 1_000_000:
        print("cache hit: %s" % cache_path)
        return cache_path
    print("downloading %s ..." % url)
    req = urllib.request.Request(url, headers={"User-Agent": "EQMap/1.0"})
    with urllib.request.urlopen(req, timeout=180) as resp, open(cache_path, "wb") as f:
        f.write(resp.read())
    print("saved %s (%.1f MB)" % (cache_path, os.path.getsize(cache_path) / 1e6))
    return cache_path


def rings_of(geometry):
    """Polygon / MultiPolygon から外環・内環をまとめてリングの列として取り出す。"""
    gtype = geometry["type"]
    coords = geometry["coordinates"]
    if gtype == "Polygon":
        polygons = [coords]
    elif gtype == "MultiPolygon":
        polygons = coords
    else:
        raise ValueError("unexpected geometry type: %s" % gtype)
    for polygon in polygons:
        for ring in polygon:
            yield ring


def _perpendicular_distance_sq(pt, start, end):
    """線分 start-end から pt までの距離の2乗。度をそのまま平面として扱う。"""
    x, y = pt
    x1, y1 = start
    x2, y2 = end
    dx, dy = x2 - x1, y2 - y1
    if dx == 0.0 and dy == 0.0:
        return (x - x1) ** 2 + (y - y1) ** 2
    t = ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    px, py = x1 + t * dx, y1 + t * dy
    return (x - px) ** 2 + (y - py) ** 2


def simplify(points, tolerance):
    """Douglas-Peucker。再帰ではなくスタックで回す（長いリングでも安全）。"""
    if len(points) < 3:
        return list(points)
    tol_sq = tolerance * tolerance
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        max_dist_sq, index = -1.0, first
        for i in range(first + 1, last):
            d = _perpendicular_distance_sq(points[i], points[first], points[last])
            if d > max_dist_sq:
                max_dist_sq, index = d, i
        if max_dist_sq > tol_sq:
            keep[index] = True
            stack.append((first, index))
            stack.append((index, last))
    return [p for p, k in zip(points, keep) if k]


def ring_extent(ring):
    """リングの経度幅・緯度幅の大きい方。極小の岩礁を捨てる判定に使う。"""
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    return max(max(lons) - min(lons), max(lats) - min(lats))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tol", type=float, default=0.01,
                        help="Douglas-Peucker の許容誤差（度）。大きいほど粗い。既定 0.01")
    parser.add_argument("--min-extent", type=float, default=0.02,
                        help="この幅（度）未満のリングは捨てる。既定 0.02")
    parser.add_argument("--digits", type=int, default=3,
                        help="座標の小数桁数。既定 3（約100m精度）")
    args = parser.parse_args()

    path = download(SOURCE_URL, CACHE_PATH)
    print("parsing ...")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    japan = None
    for feature in data["features"]:
        props = feature.get("properties", {})
        if props.get("ADM0_A3") == "JPN" or props.get("ISO_A3") == "JPN":
            japan = feature
            break
    if japan is None:
        sys.exit("ERROR: Japan feature not found in source data")

    raw_rings = list(rings_of(japan["geometry"]))
    raw_points = sum(len(r) for r in raw_rings)

    out_rings = []
    for ring in raw_rings:
        if ring_extent(ring) < args.min_extent:
            continue
        simplified = simplify(ring, args.tol)
        if len(simplified) < 3:
            continue
        # 小数桁を落とす。同じ座標が連続したら潰す。
        quantized = []
        for lon, lat in simplified:
            point = [round(lon, args.digits), round(lat, args.digits)]
            if not quantized or quantized[-1] != point:
                quantized.append(point)
        if len(quantized) >= 3:
            out_rings.append(quantized)

    out_rings.sort(key=len, reverse=True)
    kept_points = sum(len(r) for r in out_rings)

    body = ",".join(
        "[" + ",".join("[%s,%s]" % (p[0], p[1]) for p in ring) + "]"
        for ring in out_rings
    )
    js = "const JAPAN_GEO=[%s];\n" % body
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(js)

    print("rings  : %d -> %d" % (len(raw_rings), len(out_rings)))
    print("points : %d -> %d" % (raw_points, kept_points))
    print("output : %s (%.1f KB)" % (OUT_PATH, os.path.getsize(OUT_PATH) / 1024))
    if os.path.getsize(OUT_PATH) > 120 * 1024:
        print("NOTE: 120KB を超えています。--tol を上げて再実行してください。")


if __name__ == "__main__":
    main()

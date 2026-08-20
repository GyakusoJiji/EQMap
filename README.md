# EQMap

気象庁の震源データを日本地図上に時系列アニメーションで表示する。

正となる DB は Linux サーバー上の SQLite にあり、**毎日1回 自動で気象庁から取り込んで蓄積する**。
ブラウザアプリはそのサーバーを見に行く。サーバーが無くても単体で動く。

```
  気象庁 list.json ──毎日1回(systemd timer)──▶ Linux サーバー
                                                 SQLite + HTTP API + 画面配信
                                                        │
                                       ブラウザ ◀────────┘  http://<サーバー>:8787/
                                       （localStorage はオフライン用キャッシュ）
```

## 構成は2通り

### A. サーバーモード（推奨）

ブラウザで `http://<サーバー>:8787/` を開くだけ。アプリ本体も API も同じサーバーが配るので
CORS の設定はいらない。蓄積はサーバー側に貯まるので、**PC を開いていなくてもデータは増え続ける**。

設置は [server/README を兼ねた下記の手順](#linux-サーバーへの設置) を参照。

### B. 単体モード

`index.html` をダブルクリックして開く。サーバーが無い場合はアプリが気象庁を直接読み、
ブラウザの localStorage に貯める。ビルドもサーバも不要。

`file://` で開いたまま既存のサーバーを見たい場合は、URL に `?api=http://<サーバー>:8787` を付ける
（一度付ければ記憶される。サーバー側を `--cors` 付きで起動しておくこと）。

## 操作

| 操作 | 内容 |
|---|---|
| ▶ / ⏸（Space キー） | 再生 / 一時停止 |
| シークバー | 任意の時刻へ移動 |
| 速度 | 実時間1秒あたり何時間ぶん進むか |
| 期間 | 直近 7 / 30 / 90 / 365 日、全期間 |
| 規模 | マグニチュード下限で絞り込み |
| マウスホイール | ズーム（カーソル位置を中心に） |
| ドラッグ | 地図を移動 |
| 震源にホバー | 震源地名・発生時刻・M・深さ・最大震度 |
| 全体表示 | 地図の表示範囲をリセット |
| 更新 | 気象庁から手動で再取得 |
| 書き出し / 読み込み | 蓄積データを JSON で保存・復元 |

## 動作

### 起動時

1. localStorage のキャッシュから即座に地図を描く（サーバーやネットワークを待たない）
2. 裏でデータ源に問い合わせる
   - サーバーモード: `POST /api/refresh` でサーバーに最新確認をさせ、`GET /api/events` で読み直す
   - 単体モード: 気象庁を直接読んで localStorage へマージ
3. 再生は自動では始まらない。▶ を押して開始する

左上の「データ源」に、いま **サーバー / キャッシュ / 気象庁 直接** のどれを見ているかが常に出る。
接続できないときはキャッシュで表示を続ける。

### データの蓄積

気象庁が公開している一覧は **直近およそ 30 日分しかない**。取り込んでマージし続けることで、
それより古い期間もたどれるようになる。

- **サーバーモード** — サーバーの SQLite が正。systemd timer が毎日1回更新する。
  容量上限は実質なく、何年分でも貯まる。`Persistent=true` なのでサーバーが止まっていた分は
  起動後に取り返す（一覧に30日の窓があるので数日止まっても取りこぼさない）
- **単体モード** — localStorage に貯める。上限 30,000 件、超えたら古い順に削除

> **バックアップ**
> サーバーモードなら `/var/lib/eqmap/eqmap.db` をコピーするだけ。
> 単体モードは localStorage がブラウザの「閲覧データの削除」で消えるので、
> ときどき「書き出し」で JSON を保存しておくこと。

> **`file://` で開いた場合の注意**
> localStorage は Chrome / Edge / Firefox いずれでも動くが、**すべての `file://` ページで共有される**。

## データ出典

[気象庁 地震情報（多言語）](https://www.data.jma.go.jp/multi/quake/index.html?lang=jp)

同ページが内部で参照している以下の JSON を直接取得している。
`Access-Control-Allow-Origin: *` が付与されているため、ブラウザから直接読める。

```
https://www.jma.go.jp/bosai/quake/data/list.json
```

### 取り込み時の処理

- **重複排除** — 同じ地震（`eid`）に複数の報が来る。震源座標を持つ報のうち `ctt`（作成時刻）が
  最大のものを採用する
- **震度速報の除外** — `cod`（震源座標）が空なので地図に打てない。自動的に落ちる
- **座標の2形式に対応** — 通常は十進度 `+32.5+130.5-10000/`。ごく稀に度分形式
  `+3237.5+13040.7-16000/`（= 32°37.5′N / 130°40.7′E）が混ざるため、値域を超えた値は
  度分として換算する
- **深さの欠落** — 第3成分が無いレコードがある。`null`（不明）として扱う
- **遠地地震の除外** — インドネシアや南米の地震も一覧に含まれる。経度 120–156° /
  緯度 20–50° の範囲外は表示しない

## 表示

- **円の大きさ = マグニチュード**（`1.6 × 1.45^(M-2)` px）
- **発生時に点滅し、余韻を残す** — 波紋が広がったあとコアが減衰し、以降は薄い残像として
  残り続ける。再生し終えると累積した震源分布図になる
- 演出の長さは実時間基準（波紋 0.9 秒、余韻 5 秒）で定義してあるため、**再生速度を変えても
  見え方が変わらない**

## ファイル構成

```
index.html                     アプリ本体（正）。HTML / CSS / JS / 地図データを内包した1ファイル
server/
  eqmap.py                     サーバー。取得 + SQLite + HTTP API + 画面配信を1ファイルに
  install.sh                   Linux への設置スクリプト（冪等）
  web/index.html               配信用にコピーした index.html
  systemd/eqmap.service         HTTP サーバー常駐ユニット
  systemd/eqmap-update.service  取得を1回だけ走らせる oneshot ユニット
  systemd/eqmap-update.timer    毎日1回 上記を叩くタイマー
tools/make_map_data.py         同梱地図データの生成スクリプト
tools/japan.geo.js             生成された地図データ（index.html へインライン済み）
```

`index.html` を直したら `server/web/index.html` にコピーし直してから設置すること。

## Linux サーバーへの設置

Python 3.8 以上があれば動く。pip インストールは不要。

```sh
scp -r server/ ユーザー@サーバー:/tmp/eqmap-server
ssh ユーザー@サーバー 'sudo sh /tmp/eqmap-server/install.sh'
```

`install.sh` がやること:

1. システムユーザー `eqmap` を作る
2. `/opt/eqmap` にコード、`/var/lib/eqmap` に DB を置く
3. 気象庁から初回取得して DB を作る
4. systemd ユニットを入れて `eqmap.service`（HTTP）と `eqmap-update.timer`（毎日1回）を有効化する
   （systemd が無ければ `/etc/cron.d/eqmap` を代わりに置く）

冪等なので何度実行してもよい。DB は消えない。

### 運用コマンド

```sh
sudo systemctl status eqmap                     # サーバーの状態
sudo systemctl list-timers eqmap-update.timer   # 次回の自動更新はいつか
sudo journalctl -u eqmap-update -n 50           # 更新の履歴
sudo systemctl start eqmap-update               # 今すぐ1回更新する

sudo -u eqmap python3 /opt/eqmap/eqmap.py --db /var/lib/eqmap/eqmap.db stats
sudo -u eqmap python3 /opt/eqmap/eqmap.py --db /var/lib/eqmap/eqmap.db import 書き出し.json
```

### 更新タイミングを変える

`/etc/systemd/system/eqmap-update.timer` の `OnCalendar` を編集して
`sudo systemctl daemon-reload && sudo systemctl restart eqmap-update.timer`。
時刻はサーバーのローカルタイムで解釈される（`timedatectl` で確認）。

### API

| エンドポイント | 内容 |
|---|---|
| `GET /` | アプリ本体 |
| `GET /api/events` | 全イベント。`from` / `to`（epoch ミリ秒）、`min_mag`、`limit` で絞れる |
| `GET /api/status` | 件数・期間・最終取得・直近の取得ログ |
| `POST /api/refresh` | 今すぐ気象庁を見に行かせる。60秒以内の連打は 429 |

`/api/events` は `[eid, t, lat, lon, depth, mag, maxi, name]` の配列を返す。gzip 対応で、
710 件が約 11 KB。

### 地図データの再生成

[Natural Earth](https://www.naturalearthdata.com/)（Public Domain）の 10m admin_0 から日本を抽出し、
Douglas–Peucker で簡略化している。Python 標準ライブラリのみで動く。

```sh
python tools/make_map_data.py --tol 0.005
```

生成された `tools/japan.geo.js` は 1 行の `const JAPAN_GEO=[...];`。
`index.html` 内の同じ 1 行を、その中身で丸ごと置き換える。

`--tol` を上げると粗く・小さくなる（既定 0.01、現在の同梱データは 0.005 = 61 KB / 3721 点）。

元データ（13 MB）は `tools/_ne_10m_admin_0_countries.geojson` にキャッシュされる。
消しても次回実行時に再取得されるので、リポジトリには含めなくてよい（`.gitignore` 済み）。

## 制限事項

- 気象庁の JSON は公式に仕様公開された API ではないため、形式が変わる可能性がある
- 震度速報のみが発表された地震は震源が未確定のため表示されない
- 湖沼は even-odd 塗りで穴として抜いているが、簡略化の都合で小さな島や入り江は省略される

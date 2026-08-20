#!/bin/sh
# EQMap サーバーを Linux ホストへ設置する。
#
#     sudo sh install.sh
#
# 冪等。何度実行してもよい（DB は残る）。
set -eu

APP_DIR=/opt/eqmap
DATA_DIR=/var/lib/eqmap
SVC_USER=eqmap
PORT=8787
SRC="$(cd "$(dirname "$0")" && pwd)"

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "root で実行してください（sudo sh install.sh）"

command -v python3 >/dev/null 2>&1 || die "python3 がありません"
PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
say "python3 $PY_VER を使います"
python3 - <<'EOF' || die "Python 3.8 以上が必要です"
import sys
sys.exit(0 if sys.version_info >= (3, 8) else 1)
EOF

# --- サービス用ユーザー ---
if id "$SVC_USER" >/dev/null 2>&1; then
  say "ユーザー $SVC_USER は作成済み"
else
  say "システムユーザー $SVC_USER を作成"
  useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SVC_USER" 2>/dev/null \
    || adduser --system --home "$DATA_DIR" --no-create-home "$SVC_USER"
fi

# --- ファイル配置 ---
say "$APP_DIR へ配置"
mkdir -p "$APP_DIR/web" "$DATA_DIR"
install -m 0755 "$SRC/eqmap.py" "$APP_DIR/eqmap.py"
[ -f "$SRC/web/index.html" ] || die "$SRC/web/index.html がありません"
install -m 0644 "$SRC/web/index.html" "$APP_DIR/web/index.html"
if [ -f "$SRC/README.md" ]; then
  install -m 0644 "$SRC/README.md" "$APP_DIR/README.md"
fi

chown -R root:root "$APP_DIR"
chown -R "$SVC_USER":"$SVC_USER" "$DATA_DIR"
chmod 0750 "$DATA_DIR"

# --- 初回取り込み（サービス起動前に DB を作っておく） ---
say "気象庁から初回取得"
su -s /bin/sh "$SVC_USER" -c \
  "cd $DATA_DIR && python3 $APP_DIR/eqmap.py --db $DATA_DIR/eqmap.db update" || \
  say "初回取得に失敗しました（ネットワークを確認してください。タイマーが後で再試行します）"

# --- systemd ---
if command -v systemctl >/dev/null 2>&1 && [ -d /etc/systemd/system ]; then
  say "systemd ユニットを設置"
  install -m 0644 "$SRC/systemd/eqmap.service"        /etc/systemd/system/eqmap.service
  install -m 0644 "$SRC/systemd/eqmap-update.service" /etc/systemd/system/eqmap-update.service
  install -m 0644 "$SRC/systemd/eqmap-update.timer"   /etc/systemd/system/eqmap-update.timer
  systemctl daemon-reload
  systemctl enable --now eqmap.service
  systemctl enable --now eqmap-update.timer
  say "サービス状態:"
  systemctl --no-pager --lines=0 status eqmap.service || true
  say "次回の自動更新:"
  systemctl list-timers --no-pager eqmap-update.timer || true
else
  say "systemd がないため cron を設定します"
  CRON="/etc/cron.d/eqmap"
  cat > "$CRON" <<EOF
# EQMap: 毎日 04:10 に気象庁から取得して DB を更新する
10 4 * * * $SVC_USER python3 $APP_DIR/eqmap.py --db $DATA_DIR/eqmap.db update >> /var/log/eqmap-update.log 2>&1
EOF
  chmod 0644 "$CRON"
  say "$CRON を作成しました"
  say "HTTP サーバーは手動で起動してください:"
  say "  python3 $APP_DIR/eqmap.py --db $DATA_DIR/eqmap.db serve --web $APP_DIR/web"
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -n "${IP:-}" ] || IP=$(hostname)
say "完了しました  ->  http://$IP:$PORT/"
say "DB の状態:  python3 $APP_DIR/eqmap.py --db $DATA_DIR/eqmap.db stats"

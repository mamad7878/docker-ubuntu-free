#!/usr/bin/env bash
set -u

echo "======================================"
echo "     CRASH RAILWAY STARTUP"
echo "======================================"

PORT="${PORT:-6080}"
DISPLAY="${DISPLAY:-:1}"

echo "PORT    : $PORT"
echo "DISPLAY : $DISPLAY"

mkdir -p /root/.vnc

cat > /root/.vnc/xstartup <<'XSTART'
#!/bin/sh
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS
exec startxfce4
XSTART

chmod +x /root/.vnc/xstartup

echo "🖥️ Starting VNC..."

vncserver -kill "$DISPLAY" >/dev/null 2>&1 || true

vncserver "$DISPLAY" \
    -localhost no \
    -SecurityTypes None \
    -geometry 1024x768 \
    --I-KNOW-THIS-IS-INSECURE \
    >/tmp/vnc.log 2>&1 || {
        echo "❌ VNC start failed"
        cat /tmp/vnc.log
        exit 1
    }

echo "✅ VNC started"

NOVNC_PROXY=""

if [ -x /usr/share/novnc/utils/novnc_proxy ]; then
    NOVNC_PROXY="/usr/share/novnc/utils/novnc_proxy"
elif command -v novnc_proxy >/dev/null 2>&1; then
    NOVNC_PROXY="$(command -v novnc_proxy)"
fi

if [ -z "$NOVNC_PROXY" ]; then
    echo "❌ noVNC not found"
    exit 1
fi

echo "🌐 Starting noVNC on port $PORT..."

"$NOVNC_PROXY" \
    --vnc 127.0.0.1:5901 \
    --listen "$PORT" \
    >/tmp/novnc.log 2>&1 &

sleep 2

if ! pgrep -f "novnc_proxy" >/dev/null 2>&1; then
    echo "❌ noVNC failed"
    cat /tmp/novnc.log
    exit 1
fi

echo "✅ noVNC started"

echo
echo "🤖 Starting Crash Bot..."
echo "======================================"

exec python -u bot.py

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV DISPLAY=:1
ENV PATH="/opt/venv/bin:$PATH"

RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    tigervnc-standalone-server \
    tigervnc-tools \
    novnc \
    websockify \
    xfce4 \
    xfce4-terminal \
    dbus-x11 \
    xterm \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt

RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r /app/requirements.txt

COPY . /app

RUN mkdir -p /root/.vnc \
    && chmod +x /app/start.sh

EXPOSE 6080

CMD ["bash", "/app/start.sh"]

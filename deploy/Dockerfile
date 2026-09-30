# Private Robin instance. exeuntu supplies systemd and the one-shot /exe.dev/setup hook.
# Publish the result as nimul/robin:pinned. That tag is what a VM is created from.
FROM ghcr.io/boldsoftware/exeuntu:latest

LABEL exe.dev/install-shelley=false
LABEL exe.dev/login-user=exedev

USER root
EXPOSE 8000
# passwd and util-linux supply useradd and runuser. A confirmed command drops to that account.
# xvfb + fonts + libgl give headed Chromium a real display fingerprint without a monitor.
# x11vnc is optional for operators; the live view prefers Playwright screenshots over HTTP.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        passwd util-linux \
        xvfb x11vnc \
        fonts-liberation fonts-noto-core \
        libgl1 \
        wget gnupg \
    && wget -qO- https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/lib/robin/files \
    && chmod 711 /var/lib/robin/files
WORKDIR /opt/robin

COPY pyproject.toml uv.lock LICENSE README.md ./
COPY src ./src
COPY deploy/robin.service /etc/systemd/system/robin.service
COPY deploy/robin-tick.service /etc/systemd/system/robin-tick.service
COPY deploy/robin-tick.timer /etc/systemd/system/robin-tick.timer

# The unit stays disabled. exe-setup writes the enroll token, then starts it.
# Shelley and SSH stay off. No mailbox secret and no exe.dev token are baked in.
# Chromium/Chrome is for computer-use on this VM. The house server does not use this image.
# ROBIN_BROWSER_HEADLESS defaults to 0 (headed on Xvfb). Set to 1 to force headless.
# ROBIN_BROWSER_ENGINE=playwright|patchright|camoufox (patchright when installed).
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/robin/ms-playwright
ENV ROBIN_BROWSER_HEADLESS=0
RUN uv sync --frozen --no-dev --extra ner --extra stealth \
    && /opt/robin/.venv/bin/playwright install --with-deps chromium \
    && /opt/robin/.venv/bin/patchright install chromium || true \
    && ln -sf /opt/robin/.venv/bin/robin /usr/local/bin/robin \
    && rm -f /etc/systemd/system/sockets.target.wants/shelley.socket \
    && rm -f /etc/systemd/system/multi-user.target.wants/shelley.service \
    && rm -f /etc/systemd/system/multi-user.target.wants/robin.service \
    && rm -f /etc/systemd/system/timers.target.wants/robin-tick.timer \
    && rm -f /etc/systemd/system/multi-user.target.wants/ssh.service \
    && rm -f /etc/systemd/system/sockets.target.wants/ssh.socket

USER root
CMD ["/usr/local/bin/init"]

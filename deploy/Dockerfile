# Private Robin instance. exeuntu supplies systemd and the one-shot /exe.dev/setup hook.
# Publish the result as nimul/robin:pinned. That tag is what a VM is created from.
FROM ghcr.io/boldsoftware/exeuntu:latest

LABEL exe.dev/install-shelley=false
LABEL exe.dev/login-user=exedev

USER root
EXPOSE 8000
WORKDIR /opt/robin

COPY pyproject.toml uv.lock LICENSE README.md ./
COPY src ./src
COPY deploy/robin.service /etc/systemd/system/robin.service
COPY deploy/robin-tick.service /etc/systemd/system/robin-tick.service
COPY deploy/robin-tick.timer /etc/systemd/system/robin-tick.timer

# The unit stays disabled. exe-setup writes the enroll token, then starts it.
# Shelley and SSH stay off. No mailbox secret and no exe.dev token are baked in.
RUN uv sync --frozen --no-dev \
    && ln -sf /opt/robin/.venv/bin/robin /usr/local/bin/robin \
    && rm -f /etc/systemd/system/sockets.target.wants/shelley.socket \
    && rm -f /etc/systemd/system/multi-user.target.wants/shelley.service \
    && rm -f /etc/systemd/system/multi-user.target.wants/robin.service \
    && rm -f /etc/systemd/system/timers.target.wants/robin-tick.timer \
    && rm -f /etc/systemd/system/multi-user.target.wants/ssh.service \
    && rm -f /etc/systemd/system/sockets.target.wants/ssh.socket

USER root
CMD ["/usr/local/bin/init"]

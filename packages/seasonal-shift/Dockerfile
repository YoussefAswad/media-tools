FROM python:3.12-slim

RUN pip install --no-cache-dir seasonal-shift==1.2.4

ENV XDG_STATE_HOME=/state

RUN mkdir -p /config /state /data/shows

VOLUME ["/config", "/state", "/data/shows"]

ENTRYPOINT ["seasonal-shift", "watch", "--config", "/config/config.yaml"]

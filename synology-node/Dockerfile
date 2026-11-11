ARG XUI_IMAGE=ghcr.io/mhsanaei/3x-ui:latest

FROM tailscale/tailscale:latest AS tailscale

FROM ${XUI_IMAGE}

USER root

RUN apk add --no-cache \
        python3 py3-yaml \
        iptables ethtool iproute2 \
        curl bash

COPY --from=tailscale /usr/local/bin/tailscaled /usr/local/bin/tailscaled
COPY --from=tailscale /usr/local/bin/tailscale  /usr/local/bin/tailscale

COPY entrypoint.sh /entrypoint.sh
COPY configure.py   /configure.py
RUN chmod +x /entrypoint.sh /configure.py

ENV XUI_ENABLE_FAIL2BAN=false \
    XUI_DB_FOLDER=/etc/x-ui

VOLUME ["/etc/x-ui", "/var/lib/tailscale"]
EXPOSE 2053 10888 10889 10890

ENTRYPOINT ["/entrypoint.sh"]

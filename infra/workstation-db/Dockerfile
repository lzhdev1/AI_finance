FROM alpine:3.22

ARG APK_MIRROR=https://dl-cdn.alpinelinux.org/alpine
RUN sed -i "s|https://dl-cdn.alpinelinux.org/alpine|${APK_MIRROR}|g" /etc/apk/repositories \
    && apk add --no-cache openssh-client socat

COPY ssh_config /etc/ssh/workstation_config
COPY --chmod=755 start.sh connect.sh health.sh /usr/local/bin/

EXPOSE 5432
HEALTHCHECK --interval=20s --timeout=9s --start-period=10s --retries=3 \
    CMD ["/usr/local/bin/health.sh"]
ENTRYPOINT ["/usr/local/bin/start.sh"]

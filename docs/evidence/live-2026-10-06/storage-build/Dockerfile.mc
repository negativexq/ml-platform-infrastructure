FROM alpine:3.23@sha256:85fe1e81d6758c208f3e1eed4338a1997e19d4be002d4dd32d3100c9a8c010a0
COPY --chmod=755 mc /usr/local/bin/mc
USER 1000:1000
ENTRYPOINT ["mc"]

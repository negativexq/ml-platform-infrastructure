FROM --platform=$BUILDPLATFORM golang:1.26.6-bookworm@sha256:116d58cbd88c1297624acc6e967a060012422bacf9930927e23fb719189c6f36 AS build
ARG TARGETOS
ARG TARGETARCH
WORKDIR /src
COPY services/log-stream-go/go.mod services/log-stream-go/*.go ./
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -mod=readonly -trimpath -ldflags='-s -w' -o /mlp-log-stream .
FROM scratch
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=${SOURCE_REVISION}
COPY --from=build /mlp-log-stream /mlp-log-stream
USER 10001:10001
EXPOSE 8082
ENTRYPOINT ["/mlp-log-stream"]

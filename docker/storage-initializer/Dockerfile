FROM --platform=$BUILDPLATFORM golang:1.26.6-bookworm@sha256:116d58cbd88c1297624acc6e967a060012422bacf9930927e23fb719189c6f36 AS build
ARG TARGETOS
ARG TARGETARCH
WORKDIR /src
COPY services/storage-initializer-go/go.mod services/storage-initializer-go/go.sum ./
RUN go mod download
COPY services/storage-initializer-go/*.go ./
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -mod=readonly -trimpath -ldflags='-s -w' -o /storage-initializer .

FROM scratch
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=${SOURCE_REVISION}
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /storage-initializer /storage-initializer
USER 1000:1000
ENTRYPOINT ["/storage-initializer"]

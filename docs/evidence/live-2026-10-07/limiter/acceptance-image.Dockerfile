FROM localhost:5201/mlp-controlplane:limiter-dependency-base
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=${SOURCE_REVISION}
USER 0
COPY ml_platform_infra-0.1.0-py3-none-any.whl /tmp/ml_platform_infra-0.1.0-py3-none-any.whl
RUN python -m pip install --prefix=/install --no-index --no-deps --force-reinstall /tmp/ml_platform_infra-0.1.0-py3-none-any.whl && python -m pip check
USER 10001:10001

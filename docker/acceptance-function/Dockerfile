FROM python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
WORKDIR /app
COPY docker/acceptance-function/server.py ./server.py
USER 10001:10001
EXPOSE 8080
CMD ["python", "server.py"]

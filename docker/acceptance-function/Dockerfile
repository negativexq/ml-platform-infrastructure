FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3
WORKDIR /app
COPY docker/acceptance-function/server.py ./server.py
USER 10001:10001
EXPOSE 8080
CMD ["python", "server.py"]

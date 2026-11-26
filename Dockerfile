# Multi-stage, architecture-neutral image (the release workflow builds linux/arm64 natively and linux/amd64 under QEMU).
FROM python:3.13-slim AS build
WORKDIR /src
RUN python -m pip install --no-cache-dir build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m build --wheel --outdir /dist

FROM python:3.13-slim
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin kh
COPY --from=build /dist/*.whl /tmp/
RUN python -m pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl
USER kh
WORKDIR /home/kh
EXPOSE 8080
ENTRYPOINT ["kh"]
CMD ["serve"]

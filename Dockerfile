# 1 つのイメージで API とワーカーの両方を動かす（起動コマンドで役割を変える）.
#   docker build -t review-platform .
#   docker run ... review-platform api      /  review-platform worker
FROM python:3.14-slim-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# 入れ終わったら venv の pip は消す（実行時に使わない. pip が中に持つ urllib3・msgpack などの古い版が
# イメージの脆弱性の検査に出るため）
RUN pip install ".[aws,azure]" && pip uninstall -y pip
# 設計書の変換ツール（doc2md）は別の venv に入れる. ワーカーは別プロセスで呼ぶだけなので、doc-to-markdown が固定している
# devtools-common の版がこのパッケージ（auto-review と同じ版）と違ってもぶつからない. 版はコミット SHA で固定する
ARG DOC2MD_REF=56a049a43d2c34d7c17183e59555f2e81f7288b8
RUN python -m venv /opt/doc2md \
    && /opt/doc2md/bin/pip install "doc-to-markdown @ git+https://github.com/shoyo-maya-bot/doc-to-markdown.git@${DOC2MD_REF}" \
    && /opt/doc2md/bin/pip uninstall -y pip \
    && ln -s /opt/doc2md/bin/doc2md /opt/venv/bin/doc2md

FROM python:3.14-slim-bookworm
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    TMPDIR=/tmp \
    RP_HOST=0.0.0.0 \
    RP_PORT=8080 \
    RP_DATA_DIR=/data
# ベースイメージの OS パッケージを最新の修正版にする（脆弱性の修正を取り込む）
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
# ベースイメージの pip も実行時には使わないので消す（理由はビルド段と同じ）
RUN python -m pip uninstall -y --root-user-action=ignore pip
COPY --from=build /opt/venv /opt/venv
COPY --from=build /opt/doc2md /opt/doc2md
# 実行は root ではない利用者（uid 10001）. /tmp は誰でも書ける（ECS・Container Apps で読み取り専用のルートにしても動くように）
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown 10001:10001 /data && chmod 1777 /tmp
COPY scripts/init_emulators.py /app/scripts/init_emulators.py
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"RP_PORT\",\"8080\")}/healthz',timeout=3)"]
ENTRYPOINT ["review-platform"]
CMD ["api"]

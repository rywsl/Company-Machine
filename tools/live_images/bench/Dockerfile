# 现场测试镜像 mao-test/live-bench:dev：厂长会话与 A 形态工位共用的工作台（spec/system/live.md 4.1）。
# 含 git 与 python3；/workspace 属主是沙箱用户 10001，用镜像内容初始化的工作区卷对沙箱用户可写。
# BASE 由 tools/build_live_images.py 按 tools/live_versions.json 传入，必须带 digest。
ARG BASE
FROM ${BASE}
RUN apt-get update \
 && apt-get install -y --no-install-recommends git python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /workspace/drafts \
 && chown -R 10001:10001 /workspace
WORKDIR /workspace

# 夹具镜像：智能体原生工作区 studio@1。工作区在 /workspace，属主是沙箱用户 10001，
# 这样用镜像内容初始化的 workspace 卷对沙箱用户可写（spec/system/sandbox.md 第 5.4 节）。
ARG BASE=python:3.12-alpine
FROM ${BASE}
COPY README.factory /workspace/README.factory
RUN mkdir -p /workspace/drafts && chown -R 10001:10001 /workspace && chmod -R u+rwX /workspace
WORKDIR /workspace

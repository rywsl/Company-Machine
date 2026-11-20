# 夹具镜像：操作工注入卷的来源。/opt/operator 下的内容会被复制进命名卷，
# 再以只读方式挂进机器容器（spec/system/sandbox.md 第 5.5 节）。镜像必须带 /bin/sh。
ARG BASE=oven/bun:1.3.14-alpine
FROM ${BASE}
COPY fakes/fake_agent.ts /opt/operator/bin/fake-agent
RUN chmod 0755 /opt/operator/bin/fake-agent

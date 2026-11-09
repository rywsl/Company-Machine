# 夹具镜像：操作工注入卷的来源。/opt/operator 下的内容会被复制进命名卷，
# 再以只读方式挂进机器容器（spec/system/sandbox.md 第 5.5 节）。镜像必须带 /bin/sh。
ARG BASE=python:3.12-alpine
FROM ${BASE}
COPY fakes/fake_agent.py /opt/operator/bin/fake-agent
RUN chmod 0755 /opt/operator/bin/fake-agent

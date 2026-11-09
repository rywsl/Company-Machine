# 夹具镜像：工具型机器 toolbox@1 与全部夹具 validator（spec/system/testing.md 第 5 节）。
# 构建上下文由 tools/build_test_images.py 组装：本目录 + tests/fakes（放在 fakes/ 下）。
ARG BASE=python:3.12-alpine
FROM ${BASE}
COPY fakes/tools/upper_text.py /usr/local/bin/upper-text
COPY fakes/tools/count_text.py /usr/local/bin/count-text
COPY fakes/tools/check_task.py /usr/local/bin/check-task
RUN chmod 0755 /usr/local/bin/upper-text /usr/local/bin/count-text /usr/local/bin/check-task

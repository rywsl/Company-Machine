# 现场测试镜像 mao-test/claude-code:dev：Claude Code 注入卷的来源（spec/system/live.md 4.1）。
# /opt/operator 被复制进命名卷后只读挂进机器容器。node 与 CLI 都放在 /opt/operator 下，
# 机器镜像不需要自带 node，但必须是 glibc 系统（live-bench 是 Debian）。
# 入口必须是 /opt/operator/bin/claude（spec/runtimes/claude-code.yaml）。固定版本的包布局变了，就改这里。
ARG BASE
FROM ${BASE}
ARG CLAUDE_CODE_VERSION
RUN test -n "${CLAUDE_CODE_VERSION}" \
 && npm install -g --prefix /opt/operator/lib/npm "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && mkdir -p /opt/operator/bin /opt/operator/node/bin \
 && cp "$(command -v node)" /opt/operator/node/bin/node \
 && printf '#!/bin/sh\nexec /opt/operator/node/bin/node /opt/operator/lib/npm/lib/node_modules/@anthropic-ai/claude-code/cli.js "$@"\n' > /opt/operator/bin/claude \
 && chmod -R a+rX /opt/operator \
 && chmod 0755 /opt/operator/bin/claude \
 && /opt/operator/bin/claude --version

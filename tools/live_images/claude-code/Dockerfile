# 现场测试镜像 mao-test/claude-code:dev：Claude Code 注入卷的来源（spec/system/live.md 4.1）。
# /opt/operator 被复制进命名卷后只读挂进机器容器。2.1.322 的入口是 glibc 原生二进制，
# 机器镜像必须是 glibc 系统（live-bench 是 Debian）。
# 入口必须是 /opt/operator/bin/claude（spec/runtimes/claude-code.yaml）。固定版本的包布局变了，就改这里。
ARG BASE
FROM ${BASE}
ARG CLAUDE_CODE_VERSION
# 2.1.322 不再发布 cli.js，入口是平台包里的 glibc 原生二进制。
RUN test -n "${CLAUDE_CODE_VERSION}" \
 && npm install -g --registry https://registry.npmmirror.com --prefix /opt/operator/lib/npm "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && bin="$(find /opt/operator/lib/npm -type f -name claude -path '*claude-code-linux-*' | head -n 1)" \
 && test -n "$bin" \
 && mkdir -p /opt/operator/bin \
 && cp "$bin" /opt/operator/lib/claude-bin \
 && printf '%s\n' '#!/bin/sh' 'export CLAUDE_CODE_UPFRONT_TOOLS="${CLAUDE_CODE_UPFRONT_TOOLS:-mcp__factory__changeset_propose,mcp__factory__review_submit,mcp__factory__registry_check,mcp__factory__changeset_withdraw}"' 'exec /opt/operator/lib/claude-bin "$@"' > /opt/operator/bin/claude \
 && chmod 0755 /opt/operator/bin/claude /opt/operator/lib/claude-bin \
 && /opt/operator/bin/claude --version

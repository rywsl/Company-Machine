# 现场测试镜像 mao-test/codex:dev：Codex CLI 注入卷的来源（spec/system/live.md 4.1）。
# npm 包里带有静态链接（musl）的原生二进制，直接复制到 /opt/operator/bin/codex，不依赖 node。
# 入口必须是 /opt/operator/bin/codex（spec/runtimes/codex.yaml）。固定版本的包布局变了，就改这里。
ARG BASE
FROM ${BASE}
ARG CODEX_VERSION
RUN test -n "${CODEX_VERSION}" \
 && npm install -g --registry https://registry.npmmirror.com --prefix /tmp/codex "@openai/codex@${CODEX_VERSION}" \
 && bin="$(find /tmp/codex -type f -name codex -path "*$(uname -m)-unknown-linux-musl*" | head -n 1)" \
 && test -n "$bin" \
 && mkdir -p /opt/operator/bin \
 && cp "$bin" /opt/operator/bin/codex \
 && chmod 0755 /opt/operator/bin/codex \
 && /opt/operator/bin/codex --version

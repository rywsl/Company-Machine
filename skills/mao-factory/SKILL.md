---
name: mao-factory
description: 把宏观目标交给已安装的工厂，并查看状态。不代替厂内岗位执行业务。
version: 0.1.0
---

# 工厂运营

这个 skill 面向工厂运营。它区分 `factory`、`factoryctl`、`mrl` 和 harness，也区分 resident、local-test、受限外部操作者、可信管理面和厂内岗位。

外部操作者只转达宏观目标、查看状态，并把真正需要人的事项交回可信管理面。不要代为派工、签核、授权目标或重试未知的外部提交。

先运行 `factory doctor --profile local-test --json`。需要安装时，由可信管理员运行 `factory init`。目标交接使用 `factory talk send`。结果用 `factory objective ls` 和 `factory job show` 查看。未知结果用 `factory guide recovery --json`，不要盲目重新提交。

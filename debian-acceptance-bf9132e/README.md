# Debian 验收交付包

先阅读 acceptance-report.md，再查看 acceptance_buglist.md 和 findings/。
目标提交 bf9132e，实际 alpha7-7 的 13 个提交已由用户确认。
原始五项复现修正；扩展四项失败，CI 两分片 18 失败，本批未完整通过。

source/ 是该提交全部 780 个跟踪文件的字节冻结，source-inventory.json 保存哈希。
它是未修复的验收目标，复制源码不等于本轮重新审查全部文件。
original-evidence/ 保留上轮原始失败证据，baseline-replay/ 是本次重放。
independent-results/ 是最终探针；first-pass 和 exploration 目录说明非最终调度，保留原失败。
fixtures/ 保存 Tasks 恢复前后原始字节。CI 原始 shard 日志未用重跑替换。
ci-*.html 是 GitHub 公开页面，不能当作远端完整 Actions stdout。

复现使用 Python 3.14 和项目开发依赖，设置 PYTHONPATH 为目标仓库。
先复制探针到新的临时目录，再调整脚本 REPO 常量，保留本包证据原件。
independent_checks.py --case <名称> 可逐项复现，不启动 COMSOL。
CI__BUG-053__semantic_startup_probe.py 用 --repository 指定基线或目标，需同步 PYTHONPATH。
CI__BUG-031__schema_comparison.py 生成实际 schema 与完整差异，不改仓库 snapshot。
run_dependency_shards.py 与 report 中 CI gate 命令重放会运行有限无求解器测试。
执行环境记录与每次 command/cwd/时长在对应 JSON receipt。
探针 exit=0 可能表示成功观察到错误，需检查具体检查项和 functional_acceptance_passed。

provenance.json 标记复制来源，SHA256SUMS.txt 覆盖除自身外所有成员，ZIP 外另附 SHA256。
未打包 venv、node_modules、缓存、coverage 二进制、测试临时数据、DSH_HOME、API key 或许可证资产。

# 第三轮 Debian 验收证据

目标 c023ba9。原 ACC-001..009 本范围通过，ACC-010 / BUG-052 未解决。
先读 acceptance-report.md、acceptance_buglist.md 与逐文件 plan.md。
source 是固定 Git 提交快照；780 文件复制校验不表示本轮重新审查全部文件。
原件和前轮失败日志分别保存，当前成功日志没有覆盖失败原件。
concurrent_checks.py 的同一调度在上轮失败、当前通过；旧持锁回调等待写者调度已明确标注不适用于新事务。
BUG-052__late_detached_child.json 的 passed=false，即使探针 exit=0。
复现脚本内 REPO 为云端 checkout 路径，迁移到其他机器时只替换源码路径，在独立目录运行。
未启动 COMSOL，没有 private 模型、手册、密钥、venv 或缓存打包。
quality 首次缺少 ASCII test root 是环境调用错误，配置重跑单独保存。
公开 HTML 不是远端 raw CI 日志。
SHA256SUMS.txt 用于包内核验，provenance.json 记录复制来源。

# 第二轮 Debian 修复验收证据

固定目标 42f9e95，整体未通过，ACC-005/008 本范围通过，原六条仍未解决，另记录间歇故障 ACC-009。
从 acceptance-report.md 开始阅读，问题六字段见 acceptance_buglist.md。
plan.md 记录三个修改文件的核对范围。
source/ 为固定 Git 提交原始源码，不代表重新审查全部文件。
previous-failed-evidence/ 保留此前失败日志。当前结果目录保留本轮原始 stdout/stderr。
复现脚本内 REPO 指向云端 worktree；外部复验时只替换源码路径，在独立目录执行。
semantic 探针支持 --repository，写者保持运行验证短行及时返回。
未启动 COMSOL。solver engine 为确定性替身，durable IO 使用真实文件系统。
公开 GitHub HTML 不是远端完整 CI 日志。
SHA256SUMS.txt 用于核对所有包内文件，provenance.json 记录复制来源。

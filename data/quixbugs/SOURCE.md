# QuixBugs 数据来源

本目录包含 Phase 2 使用的 5 个开发任务、Phase 6 冻结的 10 个未使用测试任务，
以及闭环扩展评测使用的 5 个公开任务。

- 上游仓库：https://github.com/jkoppel/QuixBugs
- 固定提交：`4257f44b0ff1181dedaedee6a447e133219fcebf`
- 上游许可：MIT，见本目录 `LICENSE`
- 程序：保留上游缺陷版本的核心函数；
- 测试：将上游 `json_testcases` 中的公开用例改写为独立 pytest 文件；
- 参考修复和 `correct_python_programs` 未复制到本仓库，也不会提供给 Agent。
- `phase6_suite.json` 固定开发集、测试集、随机种子和任务注释；正式结果只统计测试集。

每个任务使用独立小型仓库，避免某个任务的已知缺陷污染另一个任务的完整回归测试。

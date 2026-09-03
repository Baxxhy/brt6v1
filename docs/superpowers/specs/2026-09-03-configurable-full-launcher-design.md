# 可配置全量实验启动器设计

## 目标

提供一个统一命令，在独立 tmux 会话中从头运行完整 Semantic Delta 实验，并在生成完成后自动进入官方评测。启动器只负责参数校验、环境变量映射、结果目录命名和后台启动，不复制生成或评测逻辑。

## 接口

入口为 `scripts/launch_semantic_delta_full.sh`。支持选择数据集、模型提供方、具体模型、最大语义迭代轮数、IssueRewrite/生成/评测并发数、F2P-only 或 Patch Coverage、运行名和 tmux 会话名。

向外部模型发送 Issue、检索代码和测试片段前，调用方必须显式传入 `--allow-external-model-data`。脏工作区默认拒绝；开发实验必须显式传入 `--allow-dirty`。

## 数据流

启动器解析并校验参数，创建唯一的运行标识，随后将配置映射为现有 `run_p0_simple_llm_selector_full.sh` 所支持的环境变量。现有脚本继续负责 IssueRewrite、Top-3 seed、Semantic Delta、最多 N 轮反馈、生成门禁和官方评测。

## 错误处理

缺失授权参数、非法并发数、最大轮数不在 1 到 5、运行目录已存在、tmux 会话重名或输入文件缺失时，启动器在启动前失败。启动成功后打印会话、运行目录和日志路径。

## 验证

只进行 `bash -n`、`--help` 和后台进程的启动阶段检查。真正的算法有效性由此次全量生成和官方评测验证。

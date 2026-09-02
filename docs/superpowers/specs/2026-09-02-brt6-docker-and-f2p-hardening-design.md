# BRT6 Docker 与 F2P 可靠性加固设计

## 目标

在不读取 gold patch、不改变 BRT 算法评测定义的前提下，消除重复安装、离线依赖失败、旧 pytest 漏判和明显无因果关系的生成测试。所有 Docker 数据、运行状态、锁和日志均保存在 `/root` 下。

## Docker 生命周期

- 官方缓存镜像只读复用，不构建、不拉取、不删除。
- SWT 官方 harness 固定提交及 276 条实例计算镜像 key 所需的 requirements 元数据均保存在 `evaluation/vendor/`，默认评测不读取项目外工作区。
- 每个实例使用常驻容器，容器名称完全沿用 SWT-Bench 官方 `ExecSpec.get_instance_container_name()`，不维护 BRT6 私有命名规则。
- 六个 SWT 状态以及后续相同实例重跑复用该容器。每个状态开始前执行仓库复位、普通未跟踪文件清理和残留补丁清理；使用 `git clean -fd` 保留官方镜像中被忽略的编译产物。
- cached-image 评测不再执行项目 `pip install`，不升级 pip、setuptools 或项目依赖。这与 iCoRe native 的镜像复用原则一致，并避免 PEP 517 build isolation 访问网络。
- 容器复用前校验镜像标识和运行状态。不匹配或容器损坏时，只删除该精确容器并从已有缓存镜像创建；不得拉取、构建或联网安装。
- 同一实例使用文件锁串行访问，不同实例仍可并发。锁文件放在 `/root/Baxxhy/BugReproduce/brt6/.runtime/locks`。

## 评测兼容

- 保留官方 parser 的正常结果。
- 仅当官方 pytest parser 返回空结果、日志确认恰好收集一个测试且终端摘要明确为一个 passed/failed/error 时，使用测试文件路径作为稳定 case ID 补充状态。
- 多测试、混合结果、没有收集计数或输出不完整时不猜测，继续标记 unresolved。

## 生成语义门禁

- 不使用 gold patch 或 gold test。
- 原始 Issue 中明确出现的限定 API（例如 `models.FilePathField`）优先于相似测试检索结果；候选使用冲突 API 时要求修复。
- Issue 表达“默认、未配置、absence of explicitly configured”时，候选不得显式覆盖同名配置来模拟默认行为。
- 首个失败必须是 Issue 承诺行为的可观察结果。Issue 未承诺的类型前置断言、私有实现断言或未命中目标 API 的失败不能进入 hard-eligible 候选。
- 确定性语义风险优先于 LLM `accept`，高风险候选继续修复或切换 seed。

## 错误处理

- 启动前检查 Docker socket、daemon、data-root、overlay2、可用空间和所需缓存镜像。
- Docker 或镜像前置条件失败时立即停止，不产生伪 F2P 结果。
- 容器恢复只允许一次精确重建；若仍失败，记录具体阶段并停止该实例，不循环重试安装。
- 所有诊断记录实例 ID、官方容器名和失败阶段；BRT6 不额外生成哈希标识。

## 验证

- 单元测试覆盖离线 eval 命令、容器命名/复用/损坏恢复、旧 pytest 单测试摘要和语义门禁。
- 先运行新增测试确认红灯，再实现最小修复并确认绿灯。
- 运行 BRT6 完整单测。
- 对已运行的 10 个实例做官方评测回归；重点确认 Astropy 4.3/5.x 不再安装、7746 被识别、常驻容器可复用且状态隔离。

## 非目标

- 不保证 Docker daemon、磁盘或硬件永不故障；这些外部故障必须被前置检测并安全停止。
- 不通过 gold patch 选择生成候选。
- 不修改官方数据集、gold patch、覆盖率定义或 F2P 判定公式。

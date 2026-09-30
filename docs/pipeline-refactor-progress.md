# Pipeline 架构重构验收台账

## 目标与授权

- 完整目标：对原始 8887 行 `pipeline.py` 做行为保持型架构重构，使领域责任明确，保持公开 `PipelineManager` 构造/API、已有模块导出、持久化字段及运行语义。
- 原始基线：`67de1e069eb96f398f632659626eac846b372847`。
- 隔离工作树：`C:\Users\O4\.codex\worktrees\pipeline-refactor\Mode2_AutoTranslator_MVP_public_release_candidate_20260927`；分支 `codex/pipeline-refactor`。
- 用户已授权连续 Staged Refactor；每个小步交主 Agent 验收，通过后继续。此台账不替代验收。提交由主 Agent 后续指令决定。
- 红线：不改真实 `book/`、`.runtime/`、配置、模型、网络、锁/线程/事件顺序、持久化语义、错误文案或 Provider 路由；不修复既有行为差异。
- 工程依据：`code-quality-workflow`；Library 路由 `code-quality-workflow`，ST-A0，snapshot `2026-08-18`。无设计/动效候选采用。

## 原始基线证据

- 原版 `python -B -m unittest discover -s tests -p "test_*.py"`：42/42，1.179s。
- 主 Agent 独立基线：42 Python + 4 JS 通过。
- 外联断言复核：42/42，1.101s；仅 Windows asyncio 自管 socketpair 的 `127.0.0.1` connect，未发生外联。最初禁止所有 connect 的诊断会阻断此内部 socketpair，已纠正，不是业务失败。
- 数据边界：原测试使用临时目录及进程内 TestClient；新测试双 Provider 显式离线注入，socket connect 全部禁止，所有项目在 TemporaryDirectory。

## 阶段与文件归属

| 阶段 | 归属/文件 | 状态 |
| --- | --- | --- |
| A1 单元校验契约 | 实施者 A：`tests/test_pipeline_unit_contract.py` | 原逻辑 7/7；主 Agent 已独立复跑并通过测试验收 |
| A2 校验纯函数提取 | 实施者 A：`pipeline.py`、`core/unit_validation.py` | 首版被拒后已修正，等待主 Agent 验收 |
| B 概念内容领域 | 质量 Agent：其授权概念域文件、`tests/test_concept_domain_contract.py` | 主 Agent 已验收并提交 `015b772` |
| 验收台账 | 实施者 A：本文件 | 每阶段只维护此文件，不写 memory |
| 后续领域与门面收敛 | 主 Agent 分配最小范围 | 尚未验收/完成 |

## A1/A2 证据与保留行为

- 先写公开入口 characterization，再抽取实现。测试驱动 `create_project/start/get_unit/snapshot`，Event 控制双 Provider，真实 `ProjectStore.save` 完成后旁路观察 Event，未替换持久化或调用私有 worker/Future。
- 覆盖成功翻译自动 review、translation 异常、错误 unit/hash/空译文、合法 FAIL、review binding/shape/issue/verdict 拒绝顺序、review 异常。测试首次因预期哈希错误文案多一个空格失败；仅修正测试后原逻辑 7/7，0.295s。
- 首版迁后同组 7/7，0.321s；主 Agent 独立联合 67 Python/4 JS 通过，仍因 hash 提前读取差异拒绝首版，要求返工；该差异现已消除。
- 修正版两项校验接收只用于绑定读取的 `Mapping[str, Any]` 单元数据及 `result`，原 controller 直接传原 unit/current，删除旧私有方法，无 wrapper、无提前取值 copy。
- AST：两函数正文与原版完整严格匹配，无字段替换；全部160个保留方法在仅归一化 validator 归属调用后完整 AST 匹配，原 lock/save/字段读取顺序完全保持。162 方法中只删除这两个私有校验方法。
- 既有耦合保留：translation 校验位置在原锁段之后；review 在锁内验证，随后独立锁段提交；translation 排他依赖与 revision guard 的既有差异不修；各路径 save 失败行为不统一。
- 新增 characterization：错误 result unit_id 且 unit 缺失 hash 时仍优先抛原 unit_id 错误；同一测试先在 git 基线原始函数执行通过（1/1，0.002s），再在修正版执行通过。修正版完整单元契约 8/8，0.288s。
- 修正版联合 worktree 完整 Python：68/68，1.754s（包含概念 Agent 新增测试，不能归为 A 独有）。JS `node --test tests/test_session_request.cjs`：4/4。
- 内存 Python compile：52 文件；`node --check`：9 JS/CJS 文件。叶模块先导入及 app 先导入均通过；`unit_validation` 仅依赖 typing、`core.exceptions/providers.base`，无 manager 回导。
- `git diff --check` 通过；并行概念域改动属于 B，A 未写其文件。
- A 全范围 scope 工具包含新测试及台账，超过默认200行阈值；未以删去测试/台账的方式制造通过。修正版生产改动仅2文件、116行；测试及台账预算由主 Agent 显式验收。

## B 阶段主 Agent 验收回执

- 提交 `015b772`；17 个定义 AST 同构、别名身份保持；65 Python、4 JS、51 Python 内存语法及9 JS syntax通过。
- 概念领域剩余 SCC：`quality_support <-> concept_automation`，尚未解开。
- Git 提交使用命令级身份，不修改 Git 配置。A 未提交文件保留在 worktree。

## 剩余项与下一门

- 主 Agent 验收 A2 的机械移动、公开契约和边界；未验收前不进入下一批。
- B 的 content 阶段已验收，余下 SCC/概念工作流待分阶段推进。
- 后续调度、运行控制、项目/单元工作流、质量工作流等领域迁移，须逐批冻结文件归属及 characterization；不得通过万能 PipelineContext 暴露整个 manager 或 bound manager callbacks。
- shared holder 最多为 state 引用、原 RLock、store、closed；领域使用限定端口；具体设计由主 Agent 验收。
- 最终完整架构验收、真实宿主边界、Git/提交/发布均未完成。本批离线证据不得标记为 driver-accepted。

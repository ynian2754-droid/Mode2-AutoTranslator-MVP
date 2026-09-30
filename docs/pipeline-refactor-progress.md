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
| A2 校验纯函数提取 | 实施者 A：`pipeline.py`、`core/unit_validation.py` | 返工后主 Agent 验收并提交 `afba224` |
| A3 Provider routing | 实施者 A：`pipeline.py`、`core/provider_routing.py`、A单元契约测试 | 主 Agent已验收并提交 `dd59816` |
| A4 项目四资源cell | 实施者 A：`pipeline.py`、`core/project_state.py`、`tests/test_pipeline_state_contract.py` | 实施及联合验证通过，冻结供主 Agent提交 |
| B 概念内容领域 | 质量 Agent：其授权概念域文件、`tests/test_concept_domain_contract.py` | content `015b772`；candidate/消环 `57ff5e5` 均已验收提交 |
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
- A 全范围 scope 工具包含新测试及台账，超过默认200行阈值；未以删去测试/台账的方式制造通过。修正版生产改动仅2文件、116行；主 Agent 已接受441行含新测试和台账的预算并验收提交 `afba224`。

## B 阶段主 Agent 验收回执

- 提交 `015b772`；17 个定义 AST 同构、别名身份保持；65 Python、4 JS、51 Python 内存语法及9 JS syntax通过。
- candidate/消环提交 `57ff5e5`；主 Agent 独立联合68 Python/4 JS通过，运行时含延迟导入 SCC=[]。importers 的环仅为 TYPE_CHECKING 边，不是运行时环。
- Git 提交使用命令级身份，不修改 Git 配置。

## A3 Provider routing 验收证据

- 原实现先新增5项公开入口 characterization，13/13（0.407s）；主 Agent 原实现独立13/13（0.390s）后允许迁移。覆盖六task的override/group fallback、unit partial真实defaults、quality partial共享fake与tuple冻结、falsey unit注入、default构造期间注入更新时点。
- 独立 `ProviderBindings` 是六注入字段的唯一owner；六个明确property保持已有注入API，无万能代理或mirror。`ConfigPort` 仅 `config_for_task`，UnitFactories四项、QualityFactories五项，保留pipeline模块constructor patch。Prompt仍在request构造时冻结。
- defaults及task选择归属 `core/provider_routing.py`；两个controller桥保留原provider_name读取lock、quality injected tuple在lock前冻结。cell尚未引入，避免提前冻结state引用；不向路由传manager、globals或完整state字典。
- 原unit/quality路由逻辑仅bindings/settings/factory归属替换后AST严格匹配；两桥原锁及读取前缀AST严格匹配；全部其他旧方法AST不变，constructor仅六字段赋值替换成bindings构造。
- 迁后单元契约13/13（0.386s）；联合85 Python（1.630s）/4 JS、54 Python内存compile/9 JS syntax、leaf-first/app-first import、diff检查通过；联合数包含B的12项quality公开测试。
- 主 Agent独立85 Python/4 JS、54 Python/9 JS syntax、eager及deferred运行时SCC=[]通过；实际diff、六property兼容及业务时点均已验收。

## A4 ProjectStateCell 基础批证据

- 先写6项characterization：原版6/6（0.039s）；主 Agent独立原版6/6（0.040s）后批准迁移。覆盖current state替换snapshot/request/output、save失败原异常传播且不回滚项目替换、closed晚到save跳过、active close拒绝后idle关闭、原RLock/store identity及重入、events clock/details/160条裁剪。
- `ProjectStateCell`严格四字段 `state/lock/store/closed`，在原lock构造之后初始化；state与_closed两个明确property，无mirror；manager lock/store仍引用原实例。原load/create/import/resegment赋值顺序保持，领域始终动态读取cell.state。
- `save_project(cell)`只有原closed guard及store.save；`append_event(cell,event_type,message,unit_id,details,clock)`只有原事件创建/追加/裁剪，显式clock继续使用调用点pipeline.now_iso，现有module patch可用。两基础函数不acquire锁、不规范化、不recompute stats、不新建事务。
- 原caller locks不变；全部旧方法AST除两基础write桥均不变，constructor AST仅增加cell初始化；两新基础函数正文仅cell/clock归属替换后与原版严格一致。运行验证四字段、同lock/store identity、state/_closed不在manager.__dict__。
- 迁后专用6/6（0.047s）；联合98 Python（1.955s）/4 JS、57 Python内存compile/9 JS syntax、两种fresh import顺序、diff检查通过，包含B prepare七项新增测试。
- 主 Agent独立实际diff及98 Python/4 JS、57 Python/9 JS syntax、runtime SCC=[]通过。本批冻结等待主 Agent提交。

## 后续cell/execution已通过的设计边界

- 唯一 `ProjectStateCell` 仅state引用、同一个原RLock、store、closed；state及_closed允许明确兼容property，领域每次读取cell.state动态看见项目替换。
- `ExecutionRuntime` 单独拥有原executor、active集合/Future/meta、cancel Event/timer/retired executor/invocation记录；不塞业务helpers，不复制runtime字典到facade。
- Scheduler调用UnitWorkflow；Workflow仅经窄cancellation/invocation端口读取运行控制，无Workflow到Scheduler反向调用；repair_control涉及业务反馈时可留Workflow。
- 小基础操作采用 `core/project_state.py` 的 `append_event`/`save_project` 函数，无需为14行event或简单save造service。save仅原closed guard及store.save，不能提前吸入normalization/stats。
- Workflow迁移前提交实际needs表，再裁决UnitData/Requests/Reference是否需要实例；纯规则优先函数及显式数据。manual/save/decide与enqueue可保留facade原锁原子段协调，不为薄而破锁。
- 现有request私有入口测试、glyph patch、ProjectSession.delete生命周期检查须继续可用；生命周期查询可以明确替代，不能用多层property复制全部runtime。

## 剩余项与下一门

- A4已冻结待提交；随后pipeline写权临时交B迁quality card workflow，A并行补unit高风险characterization、实际needs表及纯unit domain，不写pipeline直到B小批结束。
- B content、candidate、消环已验收；B 后续先补 quality 公开行为 characterization，A 独占 pipeline 写权。
- 后续调度、运行控制、项目/单元工作流、质量工作流等领域迁移，须逐批冻结文件归属及 characterization；不得通过万能 PipelineContext 暴露整个 manager 或 bound manager callbacks。
- shared holder 最多为 state 引用、原 RLock、store、closed；领域使用限定端口；具体设计由主 Agent 验收。
- 最终完整架构验收、真实宿主边界、Git/提交/发布均未完成。本批离线证据不得标记为 driver-accepted。

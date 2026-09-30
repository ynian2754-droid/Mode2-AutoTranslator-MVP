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
| A4 项目四资源cell | 实施者 A：`pipeline.py`、`core/project_state.py`、`tests/test_pipeline_state_contract.py` | 已验收提交 `ecf1b64` |
| A5 单元高风险生命周期 | 实施者 A：`tests/test_pipeline_unit_lifecycle.py` | 已验收提交 `48f04ff` |
| A6 单元反馈与请求owner | 实施者 A：`pipeline.py`、`core/unit_state.py`、`core/unit_requests.py`、`core/project_settings.py`、A state测试 | 主 Agent已复验通过，冻结供提交 |
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
- quality公开scan/retry/persistence测试提交 `429cc30`；prepare/recovery/commit七项测试提交 `3ed3ca1`。
- quality card事务/模式owner提交 `76bb7ef`；cell、quality_state和quality_cards形成明确领域边界，B文件后续由B独占。
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

## A5 单元生命周期测试证据

- 生产只读，仅新增单元生命周期测试。首批9/9（0.191s）；主 Agent要求消除私有freeze spy/alias断言，并补review stale revision，修改后10/10（0.206s）。
- 公开入口覆盖manual edit→显式recheck及revision；retry保留原draft/review/feedback经过失败再成功消费；accepted-risk仍editable但不可direct review；prompt按T/R各stage冻结；公开reference_mode在T阻塞时automatic→manual，auto review保留automatic/frozen_empty旧snapshot内容，显式recheck用manual新snapshot。
- result commit一次save失败保留原回滚差异：translation恢复feedback/manual参考且不自动review，不重调模型；review保留译文并拒绝成功verdict、不重调模型；成功事件被撤回。review返回前fixture在原lock改变revision时，PASS拒绝并以当前revision记录controller failure，原错误文案和事件保持。
- 真实OpenAI-compatible adapter、api_client和repair loop使用fake urlopen HTTP envelopes运行：translation修复一次成功再review、三轮exhaustion无review、真实RepairControl stale source或公开stop在首次HTTP前阻止调用。socket guard、TemporaryDirectory、Event，无sleep。
- A前次联合112 Python（2.087s）/4 JS、59 Python/9 JS syntax通过（含B新增卡片测试）；修正后主 Agent独立联合113 Python/4 JS、61 Python/9 JS syntax、runtime SCC=[]通过。
- 已验收，测试及本台账冻结供主 Agent独立提交，不改B窗口production。

## A6 单元反馈、请求及配置兼容规则证据

- 精确旧定义行数481：反馈/repair normalization六函数207行；request/context/role/reference/配置九函数274行，主 Agent批准同批迁移。
- 新增legacy load characterization先原版7/7（0.054s）；主 Agent独立原版7/7（0.053s）后放行，迁后7/7（0.051s）。覆盖bool revision、manual字段、suggestion去空/重、旧draft错误类型、inflight repair中断、unknownkind、bool success_round，并保留invalid-int原ValueError。
- `core/unit_state.py`六纯函数，不依赖manager；`UnitRequests(cell, settings, clock)`承接request owner，context/role/reference规则用显式数据函数，review构造先写reference后返回request的原时点保持。caller原锁、translation/review原引用及保存顺序不变。
- `configured_target_words`无现有等价函数；主 Agent明确批准 `core/project_settings.py`只承接原11行canonical/legacy读取，避免segmentation/resegment反向依赖请求领域；未新增service或改变逻辑。
- 两个旧private request入口因原测试保持明确adapter；其余13个旧规则方法移除，所有内部callsites直接转领域函数。clock为明确普通函数依赖，pipeline模块级supplier动态调用原now_iso，不闭包manager。
- 迁后10项单元生命周期通过（0.204s）；A前次联合114 Python（2.086s）/4 JS通过。主 Agent最终联合121 Python/4 JS、65 Python/9 JS syntax、runtime eager+deferred SCC=[]通过（包含B新增execution测试）。
- 15定义正文AST经显式state/settings/clock/函数归属标准化后严格一致，连原docstring值保持；所有保留方法仅直接call wiring变化，constructor仅增加Requests owner。主 Agent独立复核6 unit_state+8 requests函数同构及project_settings原11行同体，已读完整callsite wiring。
- 当前模块大小：pipeline 8240行、unit_state 223行、unit_requests 308行、project_settings 21行。无manager回导，leaf-first/app-first imports及diff检查通过。
- 剩余耦合：unit执行/repair业务仍在pipeline；provider stage仍两个controller桥；ExecutionRuntime/InvocationTracker/Scheduler尚未迁。项目配置兼容已有独立owner；Requests只持cell/settings/clock，不访问scheduler/provider。
- 本批冻结供主 Agent提交，pipeline下一窗口交B的QualityRuntime/PrepareProgress迁移。

## 后续cell/execution已通过的设计边界

- 唯一 `ProjectStateCell` 仅state引用、同一个原RLock、store、closed；state及_closed允许明确兼容property，领域每次读取cell.state动态看见项目替换。
- `ExecutionRuntime` 单独拥有原executor、active集合/Future/meta、cancel Event/timer/retired executor/invocation记录；不塞业务helpers，不复制runtime字典到facade。
- Scheduler调用UnitWorkflow；Workflow仅经窄cancellation/invocation端口读取运行控制，无Workflow到Scheduler反向调用；repair_control涉及业务反馈时可留Workflow。
- 小基础操作采用 `core/project_state.py` 的 `append_event`/`save_project` 函数，无需为14行event或简单save造service。save仅原closed guard及store.save，不能提前吸入normalization/stats。
- Workflow迁移前提交实际needs表，再裁决UnitData/Requests/Reference是否需要实例；纯规则优先函数及显式数据。manual/save/decide与enqueue可保留facade原锁原子段协调，不为薄而破锁。
- 现有request私有入口测试、glyph patch、ProjectSession.delete生命周期检查须继续可用；生命周期查询可以明确替代，不能用多层property复制全部runtime。

## UnitWorkflow 实际needs与下一批候选

| 归属 | 实际需要 | 下一步 |
| --- | --- | --- |
| UnitWorkflow | 四常驻对象：cell、UnitRequests、ProviderRouter、InvocationPort | 99行translate、117行review及其repair业务helpers；保留每个lock/provider/save时点 |
| UnitRequests | cell动态state、ApiSettings窄prompt/config读取；SOURCE context、预算、role、reference freeze/store、feedback规则 | 先迁request及必需feedback函数，不独立造Reference服务 |
| ProviderRouter | cell、bindings、settings、pipeline模块级factories supplier | stage取当前constructor，禁止bound manager callback |
| InvocationTracker | cell、ExecutionRuntime；cancel_requested/begin/end/is_current四项端口 | Workflow不持Scheduler；repair_control/progress因unit业务留Workflow |
| unit_state纯函数 | retained draft、suggestion normalize/extract、feedback backfill、find/failure/cancel/rollback规则 | 显式state/unit/clock，不造常驻UnitData服务 |
| 基础写入 | save_project/append_event既有函数；普通module级clock supplier保持pipeline.now_iso patch | 不追加service、不合并领域事务 |

- 四常驻对象依赖+普通clock明确函数参数已获主 Agent裁决；clock不能闭包manager。Scheduler→Workflow→Tracker单向，manual/save/decide/retry继续facade原锁协调。
- 后续首批候选为unit_request与必要feedback/context/reference纯规则，约300旧逻辑行，最大600旧生产行；现有两个private request入口因旧测试可保持明确adapter，其他规则按实际调用点替换，不批量wrapper。

## 剩余项与下一门

- A6已验收提交 `d844984`；B质量并发七项测试已提交 `0985bd6`，QualityRuntime/PrepareProgress已提交 `5fde422`。当前pipeline由B迁质量请求/恢复纯规则，A仅写execution characterization与本台账，生产窗口由主 Agent协调。
- B content、candidate、消环已验收；B 后续先补 quality 公开行为 characterization，A 独占 pipeline 写权。
- 后续调度、运行控制、项目/单元工作流、质量工作流等领域迁移，须逐批冻结文件归属及 characterization；不得通过万能 PipelineContext 暴露整个 manager 或 bound manager callbacks。
- shared holder 最多为 state 引用、原 RLock、store、closed；领域使用限定端口；具体设计由主 Agent 验收。
- 最终完整架构验收、真实宿主边界、Git/提交/发布均未完成。本批离线证据不得标记为 driver-accepted。

## A7 执行生命周期首批 characterization

- 新增 `tests/test_pipeline_execution_contract.py` 八项，生产只读。原执行逻辑8/8（0.196s）通过；主 Agent独立读全部测试并原逻辑8/8（0.139s）通过。
- 真实ThreadPoolExecutor与每unit独立Event证明两个provider同时进入、run始终包含完整scope、仅一unit完成时run继续且真实磁盘保持；fixture记录submitted Future，在原manager完成callback之后追加Event，只作同步观察，不以runtime字段镜像为核心断言。
- 取消覆盖queued任务首次provider之前、durable translation之后automatic review之前、review返回之前。分别证明未调用后续模型、旧结果丢弃、已保存译文及revision保留，磁盘和公开状态一致。
- fake Timer捕获原5s宽限期并手动触发，无sleep。旧worker占unit时拒绝重入；其他unit可起新run；旧worker迟到callback与旧Timer重复回调均不能结束或改变新run scope，旧译文不会导入。
- 精确submit故障保留revision回滚与active清理，以及原异常路径未进行最终save的内存/磁盘差异。已完成Future同步callback证明首次submit前完整scope已冻结，第一unit完成不提前结束run。
- manager.close与ProjectSession.delete在active及retired worker期间拒绝；迟到worker完全收尾后删除成功，关闭manager拒绝新任务，项目目录不被重建。核心观察为公开run/unit状态、provider调用、真实磁盘及事件。
- A联合129 Python（2.373s）/4 JS通过；70 Python内存compile/9 JS syntax、fresh leaf-first/app-first imports、含延迟导入且排除TYPE_CHECKING的runtime SCC=[]、git diff --check通过。计数包含B当前质量纯规则工作树改动。
- 全部数据TemporaryDirectory，socket connect guard，translation与review明确注入离线provider。生产文件未写；本批测试和台账冻结供主 Agent验收提交。
- 未覆盖的更大scheduler并发矩阵留后续Scheduler批；下一步等待明确生产窗口后实施ExecutionRuntime/InvocationTracker及完整ProviderRouter，不迁unit业务或quality业务到运行时资源底座。

## A8 ExecutionRuntime / InvocationTracker / ProviderRouter 基础批

- A7 execution八项已验收提交 `dfc3a96`；B质量请求/恢复规则已提交 `476a6ce`，editorial五项测试已独立提交 `a897110`。
- 生产四文件scope：pipeline、core/execution_runtime新模块、core/provider_routing、core/project_catalog仅delete query wiring；未写B领域文件或新增测试。
- ExecutionRuntime唯一拥有原九项executor/并发数、active unit/Future/meta、cancel Event、stop Timer、retired executor、invocation map。所有内部属性直接转owner，不加property或镜像集合。
- Tracker四方法16旧行；Router两stage方法44旧行。Tracker不增减锁；Router保留unit原lock与动态注入读取、quality锁前冻结四provider tuple以及partial fake默认语义。pipeline模块级id/factories supplier动态读取原uuid与九构造器，兼容既有pipeline模块patch，不闭包manager。
- 六迁移方法正文AST经显式归属/factory映射后严格一致；132保留方法正文经直接属性/调用映射后严格一致（验证器保持原Store/Load ctx），constructor除九字段替换为三个明确owner初始化外严格一致。Router原docstring值保持。
- 新has_live_work_locked仅只读原run.running/status stopping与active ids/meta/retired三项综合谓词，无新锁。ProjectSession.delete保留session→manager双锁贯穿query/delete/close；close自身仍仅原active ids条件，未增强或复用delete guard。
- 迁后8 execution（0.143s）、13 unit/routing（0.405s）、10 unit lifecycle（0.208s）通过；A联合134 Python（2.406s）/4 JS、72 Python内存compile/9 JS syntax、两种fresh import顺序、含延迟导入runtime SCC=[]、git diff --check通过。主 Agent独立完整diff/134 Python/4 JS/72 Python/9 JS/runtime SCC=[]通过。
- 模块大小：pipeline7627行、execution_runtime58行、provider_routing139行、project_catalog476行。运行资源只依赖cell基础，不依赖manager；Router无manager回调或prompt职责。
- 剩余耦合：Scheduler的submit/Future callback、stop/grace/run状态协调仍在facade；UnitWorkflow的translation/review及repair业务仍在facade。保留原线程/锁/save时点，下一批实质workflow再迁。
- 本批已验收提交 `d994d65`；B check-only349旧行已验收提交 `894be60`，A随后获得UnitWorkflow窗口。主 Agent同时核实原工作目录仍clean且HEAD=`67de1e069eb96f398f632659626eac846b372847`，全部旧PipelineManager公开方法及签名保持。

## A9 输出公开契约补充

- 等待生产窗口期间仅新增tests/test_pipeline_output_contract.py五项，复用明确离线ControlledProvider，TemporaryDirectory真实文件与Event，不写生产。
- 原执行逻辑5/5（0.174s）；主 Agent独立5/5（0.158s）、联合139 Python/4 JS、74 Python/9 JS syntax、runtime SCC=[]通过，已独立提交 `6f4e7a3`。
- readiness覆盖passed/user_modified/accepted_risk和未完成/空译文。legacy status fixture明确标注且公开output_status观察，manual edit及accept-risk用公开入口；保留needs_action人工编辑仍needs_action的原行为。
- text与markdown真实artifact内容、hash、trace节点/unit/source绑定、磁盘metadata及最后document_exported事件；generate_output save失败保持已发布artifact与内存metadata/event、磁盘state仍旧，不重调provider。
- pending或故障exporter不追加metadata/event/持久化；替换state后glyph缓存更新U+FFFD warning，原readiness及旧返回值不受污染。未复制既有DOCX trace发布回滚测试。

## A10 UnitWorkflow 实质领域迁移

- 精确十三旧定义495行：UnitWorkflow五方法304（execute10、translate99、review117、repair_control42、repair_progress36）；unit_state八函数191（find5、cancel8、failure26、repair_payload27、repair_failure49、restore18、repair_save_failed35、repair_terminal23）。原classmethod decorator另1行，不影响600行预算。
- Workflow四明确对象cell/requests/router/InvocationPort与普通clock；Port仅cancel/begin/end/is_current四方法，无scheduler引用或manager callback。cancel/failure函数明确cell+clock调用基础append_event，其余函数显式state/unit/data；unit_state因此为领域mutation函数集合，不宣称全部纯函数。
- Scheduler仅submit目标直接转workflow.execute；facade原find/failure/cancel调用直接函数；十三旧私有定义删除，旧两个request adapter因原测试继续保留，Workflow直接requests构造。所有模型调用、原锁块、验证、提交、保存和失败回滚整体迁移，未剥离commit步骤。
- translation validation仍锁外原unit引用，最终translation commit仅原cancel guard；review validation和commit保留两次原锁，不修潜在竞态。明确AST映射now_iso→clock/self.clock，store_reference原_unit_request_clock→self.clock，二者仍动态pipeline.now_iso。
- 初版文本生成将迁移正文unparse压成长行且丢失注释，主 Agent以维护性回退拒绝。已从HEAD原文本通过token/限定调用替换重建，恢复全部多行格式、字典和原关键注释。最终十三迁移正文经显式参数/归属/clock映射AST全部同构，117保留method正文仅wiring同构，constructor仅增加Workflow；全部原docstring值及十九原内部COMMENT按序完整保留。
- A联合144 Python（2.699s）/4 JS、76 Python内存compile/9 JS syntax、fresh双顺序imports、含延迟导入runtime SCC=[]、git diff --check通过。主 Agent独立读恢复格式/ctor/diff并复核13 moved AST、保留method、19 COMMENT及144 Python/4 JS/76 Python/9 JS/runtime SCC=[]通过；B parallel五项测试独立提交 `5f316ae`，包含在联合计数。
- 本批A生产范围仅pipeline/core.unit_state/new core.unit_workflow与本台账，未写B core.quality_batches。模块最终大小pipeline6784行、unit_state431行、unit_workflow347行，不将B未接线scan变更归本批。
- 剩余耦合：scheduler的executor/Future callback、run scope、stop/grace、stats/snapshot/output invalidation与人工入口原子协调仍facade；后续Scheduler批单独迁，不为facade薄而破坏事务锁。完整架构重构仍在推进，未作宿主或driver-accepted声明。
- 本批A文件全部冻结ready，等待主 Agent独立提交；pipeline随即交B scan接线窗口，A仅准备Scheduler精确依赖清单。

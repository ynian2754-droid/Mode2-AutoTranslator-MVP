# Release notes / 更新日志

## v0.2.4 — 2026-10-01

### English

- **Refactored the translation pipeline.** The former large `pipeline.py` now delegates to focused modules for unit workflows, execution and cancellation, project state, provider routing, output, editorial suggestions, and concept preparation. The HTTP routes, project JSON format, and translation, review, retry, and export workflows remain unchanged.
- **Reduced redundant work on large concept projects.** Concept status reads reuse decisions already computed for the request, and the page skips a duplicate results refresh when loading has already supplied current data. This targets the long waits seen when opening large projects.
- **Finished English coverage on the quality results page.** Failed-batch recovery, retry controls, detailed preparation statistics, budget explanations, adoption reasons, lookup-limit notices, and concept-generation timeout errors now render in English when English is selected.
- **Kept local and development-only data out of the downloadable package.** The ZIP omits project files, runtime settings and credentials, previews, tests, and Git metadata. These files are not needed to run the Windows source package; tests remain in the GitHub source repository for contributors.

### 简体中文

- **完成翻译流水线架构重构。** 原先庞大的 `pipeline.py` 现在将单元工作流、执行与取消、项目状态、Provider 路由、输出、表达建议和概念准备交给职责明确的模块。HTTP 路由、项目 JSON 格式以及翻译、校验、重试和导出流程保持不变。
- **减少大型概念项目的重复处理。** 概念状态读取会复用当前请求已计算的辨析结果；页面加载已取得最新数据时不再重复刷新结果。这针对打开大型项目时等待时间过长的问题。
- **补齐质量结果页的英文界面。** 选择英文时，失败批次恢复、重试控件、准备详情统计、预算说明、未采用原因、补查上限提示和概念候选生成超时错误均显示为英文。
- **下载包不再携带本地数据和开发专用文件。** ZIP 排除项目文件、运行设置与凭据、预览、测试和 Git 元数据。这些文件不是运行 Windows 源码包所需；测试仍保留在 GitHub 源码仓库，供贡献者使用。

### Verification / 验证

- Python: **166 passed**, with **152 subtests passed**. One upstream Starlette deprecation warning was reported.
- JavaScript: **12 tests passed**.
- Python and JavaScript syntax checks, archive integrity, and package exclusion checks passed.
- No live model calls or clean Windows installation were performed.

### Windows source package / Windows 源码安装包

Download `Mode2-AutoTranslator-MVP-v0.2.4.zip`, extract it, install Python 3.10 or later with pip, run `安装依赖.bat`, and then run `启动.bat`. Python and model services are not bundled.

下载 `Mode2-AutoTranslator-MVP-v0.2.4.zip` 并解压，另行安装带 pip 的 Python 3.10+，运行 `安装依赖.bat` 后再运行 `启动.bat`。ZIP 不附带 Python 或模型服务。

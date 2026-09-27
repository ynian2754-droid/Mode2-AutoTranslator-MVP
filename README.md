# Mode2 AutoTranslator MVP

[English](#english) | [简体中文](#simplified-chinese)

[Download the latest release](https://github.com/ynian2754-droid/Mode2-AutoTranslator-MVP/releases/latest)

## English

Mode2 AutoTranslator is a local Windows translation workbench. It imports documents, organizes text into translation units, runs independent quality checks, and exports a complete translated document. It connects to user-configured OpenAI-compatible services; no API key or model service is bundled.

### Features

- Import PDF, EPUB, Markdown, and TXT files. Scanned PDFs need OCR before import.
- Keep source locations and content-integrity information for each unit.
- Translate units in parallel, run separate quality checks, manage terminology references, edit translations, and review accepted risks.
- Export Markdown, TXT, EPUB, and PDF.
- Use the project selector, translation workspace, concept review, and API settings pages in a local browser. The interface supports Simplified Chinese and English; Simplified Chinese is the default, and the language choice is saved in the browser.

### Windows quick start

1. Run `安装依赖.bat`. If Python 3.10 or later is not available, the installer uses the CPython installer included in the repository.
2. Run `启动.bat`.
3. Open `http://127.0.0.1:4873/` in your browser and configure your translation and review providers.

You can also run `./run.ps1` in PowerShell. To change the default port, run `启动.bat <port>`.

### User guide

See the [user guide](USER_GUIDE.md) for installation, importing, API setup, translation, review, export, and troubleshooting. The guide is currently in Simplified Chinese.

### Data and API settings

Translation projects are stored locally in `book/`; API settings are stored in `.runtime/api_settings.json`. These paths are excluded from Git. The repository and release package do not include personal projects, API keys, model accounts, or endpoint settings. When you translate or review, selected units and relevant context are sent to the provider you configured; follow that provider's data and billing terms.

### License

The project's original source code and documentation are released under the MIT License; see [LICENSE](LICENSE). The bundled CPython installer and PDF fonts retain their respective upstream licenses. See [third-party notices](THIRD_PARTY_NOTICES.md) and the font license files.

### Limitations

- Supported inputs are PDF, EPUB, Markdown, and TXT. Scanned PDFs require OCR; DOCX is not supported.
- A working OpenAI-compatible translation and review service is required. Model quality, pricing, rate limits, and availability depend on your provider.
- First-time installation and PDF export require preparing Python and dependencies as described by the installer.

---

<a id="simplified-chinese"></a>

## 简体中文

Mode2 AutoTranslator 是一款在 Windows 本机运行的翻译工作台，用于导入文档、按单元组织翻译、独立校验并导出完整译文。它通过 OpenAI-compatible 接口调用用户配置的翻译和校验服务，不附带 API Key 或模型服务。

## 功能

- 导入 PDF、EPUB、Markdown 和 TXT；扫描版 PDF 需要先完成 OCR。
- 按句子边界整理原文单元，并保留来源定位和内容校验信息。
- 并行翻译、独立质量校验、术语参考、人工编辑和风险审计。
- 导出 Markdown、TXT、EPUB 和 PDF。
- 在本机浏览器中使用项目选择页、翻译工作台、质量页和 API 设置页。

## 快速开始

1. 在 Windows 上运行 `安装依赖.bat`。电脑没有可用的 Python 3.10+ 时，安装脚本会使用仓库内的 CPython 安装程序。
2. 运行 `启动.bat`。
3. 在浏览器打开 `http://127.0.0.1:4873/`，配置翻译与校验服务后即可开始使用。

也可以在 PowerShell 中运行 `./run.ps1`。如需更换默认端口，可运行 `启动.bat 端口号`。

## 用户手册

首次安装、导入文件、配置 API、翻译与校验、导出和常见问题，请见[用户手册](USER_GUIDE.md)。

## 数据与 API 配置

翻译项目保存在本机的 `book/` 目录，API 设置保存在 `.runtime/api_settings.json`。这两个路径已加入 `.gitignore`，请勿将个人翻译项目或 API 凭据提交到公开仓库。发起翻译或校验时，所选单元及相应上下文会发送到你配置的服务商；请按服务商的数据与计费规则使用。

本仓库不含用户的翻译项目、API 密钥、模型账号或服务端点配置。

## 主要目录

```text
app.py, pipeline.py       本地 Web 应用与翻译流程
core/                     导入、分段、存储、术语、PDF/EPUB 导出
providers/                翻译、校验与 API 接口适配
web/                      HTTP API 与数据结构
static/                   浏览器页面
assets/fonts/pdf/         PDF 导出字体及对应许可
scripts/resolve_python.ps1 Python 环境解析
```

## 开源许可

本项目自有源码与文档采用 MIT License，见 [LICENSE](LICENSE)。仓库附带的 CPython 安装程序和 PDF 字体沿用各自的上游许可，不包含在项目 MIT 许可范围内；详情见 [第三方组件说明](THIRD_PARTY_NOTICES.md) 和字体目录中的许可文件。

## 当前限制

- 仅处理 PDF、EPUB、Markdown 和 TXT 输入；扫描 PDF 需要 OCR，DOCX 尚未支持。
- 需要可用的 OpenAI-compatible 翻译和校验接口；模型质量、价格、限流和可用性由所配置的服务商决定。
- 首次安装和首次导出 PDF 需要按安装脚本的说明准备 Python 与依赖。

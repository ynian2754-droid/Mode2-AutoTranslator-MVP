# Mode2 AutoTranslator MVP

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
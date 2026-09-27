# 第三方组件说明

本文件记录随仓库分发的第三方二进制和字体。它们不属于项目的 MIT License 范围。

## CPython 3.13.15（Windows x64）

仓库中的 `python-3.13.15-amd64.exe` 是未修改的 Windows 安装程序。其 SHA-256 为：

```text
edec09c4853aeae9ac36efb8c9f95b6b8e2fee65eee56d9767a8b7c69c574403
```

Python 3.13.15 的官方发行文件和校验值见 [Python.org 发布页](https://www.python.org/downloads/release/python-31315/)。CPython 按 [Python Software Foundation License Version 2](https://docs.python.org/3.13/license.html) 许可。

## PDF 字体

`assets/fonts/pdf/` 中的字体保留各自的上游许可和说明：

- `Mode2SansCJKCN-Regular.ttf`：见该目录中的 `LICENSE.txt`。
- `DejaVuSans.ttf`：见该目录中的 `LICENSE-DejaVuSans.txt`。

## Python 依赖

运行安装脚本时，pip 会根据 `requirements.txt` 安装第三方依赖。每个依赖仍受其自己的许可约束。
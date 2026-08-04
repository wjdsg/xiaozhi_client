# AI 台灯三合一

该目录已整理为可独立复制的项目，不依赖 `D:\qxyy` 下的其他本地文件。

## 快速启动

1. Windows 电脑安装 Python 3.10 或更高版本。
2. 双击 `启动台灯.bat`。
3. 首次启动会在当前目录创建 `.venv` 并安装 `requirements.txt` 中的依赖。
4. 程序启动后会自动打开 `http://127.0.0.1:8765`。

也可以在命令行中运行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run.py
```

## 转交给其他人

直接压缩并发送整个 `taideng_sanheyi` 文件夹。模型、Windows 音频 DLL、提示音、网页图片、界面脚本和图标字体均应包含在目录内。

接收者需要自行具备：

- Python 运行环境；
- 可用的麦克风和扬声器；
- 能访问配置中的 Xiaozhi WebSocket 服务；
- 首次安装 Python 包时的网络连接。

## 运行配置

远端服务、本地端口和设备标识位于 `config/runtime.json`。默认值与原项目一致。换电脑或换服务时修改该文件即可，无需改 Python 代码。

音频设备、唤醒词与 AEC 参数位于 `config/config.json`。唤醒词模型使用项目内的 `models` 目录，不应再填写外部绝对路径。

## 主要目录

- `static/`：完整网页界面和本地图标资源；
- `models/`：唤醒词模型；
- `libs/`：Opus 与 SpeexDSP Windows 动态库；
- `src/`：音频、唤醒词和工具代码；
- `config/`：运行及硬件配置。

> 远端 Xiaozhi 服务属于网络服务，不能通过复制本文件夹一并带走；接收者必须能访问 `config/runtime.json` 中配置的服务地址。

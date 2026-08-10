# AI 台灯三合一客户端

这是可以独立复制和交付的 Windows 客户端目录，不依赖 `D:\qxyy` 下的其他文件。远端 Xiaozhi 服务不包含在本目录中，使用者的电脑必须能访问配置的服务地址。

## 一、运行前准备

建议使用 64 位 Windows 10/11，以及 Python 3.10 或 3.11。Python 3.13 可能暂时没有所有音频库的可用预编译包。

1. 安装 Python 时勾选 **Add Python to PATH**。
2. 打开 PowerShell，确认版本：

   ```powershell
   python --version
   ```

3. 准备可用的麦克风和扬声器，并确认 Windows 的麦克风权限已开启。
4. 确认电脑可以访问 `config/config.json` 中的 WebSocket 服务地址。

## 二、推荐启动方式

直接双击项目根目录的 `启动台灯.bat`：

- 首次运行会在项目目录创建独立环境 `.venv`；
- 自动安装 `requirements.txt` 中的运行依赖；
- 启动本地网页服务、音频采集、AEC、唤醒词和 Xiaozhi 连接；
- 自动打开 `http://127.0.0.1:8765`。

以后再次启动仍然双击这个文件即可。关闭程序时回到命令窗口按 `Ctrl+C`，或关闭启动窗口。

## 三、命令行安装与启动

如果不使用批处理文件，可以在 PowerShell 中执行：

```powershell
cd D:\qxyy\taideng_sanheyi
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
```

如果 PowerShell 禁止执行激活脚本，不需要激活环境，直接使用上面的 `.venv\Scripts\python.exe` 即可。

## 四、依赖说明

`requirements.txt` 已覆盖客户端运行时实际使用的依赖：

- `aiohttp`、`websockets`：本地网页服务和 Xiaozhi WebSocket；
- `sounddevice`、`numpy`、`soxr`：音频采集、播放和重采样；
- `opuslib`：Opus 编解码；
- `sherpa-onnx`：本地唤醒词检测；
- `colorlog`：日志输出。

项目还自带 `libs/opus.dll`、`libs/libspeexdsp.dll`，不需要另外下载 DLL。

只有在需要运行 `gen_responses.py`、重新生成预录制语音时，才需要安装可选依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-tts.txt
```

该脚本还需要在运行前通过环境变量配置 DashScope API Key；不要把 Key 写进代码或提交到项目目录。正常启动台灯不需要 DashScope。

## 五、首次使用必须检查的配置

### 1. 远端服务配置：`config/config.json`

业务服务统一从这里读取，常用项如下：

```json
{
  "SYSTEM_OPTIONS": {
    "CLIENT_ID": "客户端唯一 ID",
    "DEVICE_ID": "设备唯一 ID",
    "NETWORK": {
      "WEBSOCKET_URL": "ws://服务器地址:5000/xiaozhi/v1/",
      "WEBSOCKET_ACCESS_TOKEN": "访问令牌",
      "OTA_VERSION_URL": "http://服务器地址:8002/xiaozhi/ota/"
    }
  }
}
```

换服务器时修改 `WEBSOCKET_URL`、`OTA_VERSION_URL` 和令牌即可。当前交付目录中的访问令牌已清空，请在使用者自己的本地配置中填写，不要把真实令牌发给他人或写入 README。

`CLIENT_ID`、`DEVICE_ID` 应保持稳定，不要每次启动都更换。

### 2. 本地网页配置：`config/runtime.json`

这里只控制本地网页监听地址和端口：

```json
{
  "local_host": "127.0.0.1",
  "local_port": 8765
}
```

如果 8765 端口被占用，可以改成其他端口，例如 8766，然后使用对应地址打开网页。远端 WebSocket 不要填写在这个文件中。

### 3. 音频、AEC 和唤醒词：`config/config.json`

- `AUDIO_DEVICES`：麦克风/扬声器设备编号、采样率和声道数；
- `AEC_OPTIONS`：回声消除参数；
- `WAKE_WORD_OPTIONS`：唤醒词开关、模型目录和阈值；
- `CAMERA`：视觉分析接口地址（使用视觉功能时才需要可访问）。

唤醒词模型必须保留在项目内的 `models/` 目录，不要改成其他电脑上的绝对路径。更换电脑后，通常需要重新设置 `AUDIO_DEVICES` 中的设备编号。

## 六、交付给他人的方式

复制或压缩整个 `taideng_sanheyi` 文件夹，至少保留以下内容：

- `run.py`、`bridge.py`、`启动台灯.bat`；
- `requirements.txt` 和 `requirements-tts.txt`；
- `config/`、`models/`、`libs/`、`src/`、`static/`；
- 根目录的 MP3 预录音文件。

不建议复制 `.venv`、`__pycache__`、`log` 和 `bridge.log`，接收者应在自己的电脑重新安装依赖。复制后只需要重新执行“运行前准备”和“推荐启动方式”。

## 七、常见问题

### 双击后提示找不到 Python

重新安装 64 位 Python，并勾选 **Add Python to PATH**，然后重新打开启动文件。

### 依赖安装失败

确认电脑可以访问 PyPI，或先手动执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 页面打不开

确认命令窗口仍在运行；检查 `config/runtime.json` 的端口是否被其他程序占用，并访问实际端口地址。

### 显示 Xiaozhi 连接失败

检查 `config/config.json` 中的 WebSocket 地址、访问令牌、电脑网络和服务器防火墙。客户端本身不包含远端 Xiaozhi 服务。

### 没有声音或唤醒无反应

检查 `AUDIO_DEVICES` 的设备编号是否属于当前电脑；可先在 Windows 声音设置中确认麦克风和扬声器能正常工作，再查看 `bridge.log`。

### 只想查看网页界面

运行：

```powershell
.\.venv\Scripts\python.exe preview.py
```

然后打开 `http://127.0.0.1:8766`。此模式不会启动麦克风、扬声器或远端语音连接。

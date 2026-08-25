import os
import re


# ================= 1. 配置参数 (集中管理，方便调优) =================

# 1.1 帧差与运动检测配置
MOTION_THRESHOLD = 30             # 像素差值阈值 (二值化用)
MIN_AREA_RATIO_MOVING = 0.005     # 判定为"正在移动"的面积占比阈值
MAX_AREA_RATIO_STILL = 0.001      # 判定为"已经停下"的面积占比阈值

# 1.2 MediaPipe 配置
MP_DETECTION_CONFIDENCE = 0.7
MP_TRACKING_CONFIDENCE = 0.5

# 手指控制：每只手 5 个手指 [小指,无名指,中指,食指,拇指]
#             左手                   右手
#             [小,无,中,食,拇]      [拇,食,中,无,小]
# 0=关闭 1=启用。被选中的手指伸直即触发，优先级：食>中>无>小>拇
# 方向向量统一使用 选定指尖 - 食指MCP，确保多指方向一致
FINGER_CONTROL = [[0, 0, 1, 1, 0], [0, 1, 1, 0, 0]]

# 手指伸直判定角度阈值（度，MCP→PIP→TIP 3D 夹角 ≥ 此值视为伸直）
FINGER_STRAIGHT_ANGLE = 155

# 拇指伸直判定角阈值（度）：MCP→IP 方向 与 MCP→CMC 方向的夹角
# 拇指伸直时该角较大，蜷曲时较小
THUMB_ABDUCTION_ANGLE = 155  # 需实测标定

# 每指 3D 归一化距离比阈值：dist3D(TIP, MCP) / dist3D(Middle_PIP, Middle_MCP)
# 顺序：[拇,食,中,无,小]
FINGER_DIST_RATIO = [0.65, 1.45, 1.35, 1.20, 1.05]  # 需实测标定


# 1.3 时序状态机与触发配置
POINTING_HOLD_TIME = 0.25         # 指向手势保持多少秒后触发 OCR
# 重触发阈值由 ROI_MODE 动态计算：选取框短边 / 2

# 1.4 坐标平滑配置 (一阶低通滤波器)
SMOOTH_ALPHA = 0.3                # 平滑系数

# 1.5 检测与识别配置
# OCR 推理后端: "paddle" | "onnx" | "onnx_quant"
# OCR 推理后端: "paddle" | "onnx" | "onnx_quant"
OCR_DET_BACKEND = "onnx"         # 检测用 ONNX（2x 提速）
OCR_REC_BACKEND = "onnx"       # 识别（长文本CPU上Paddle比ONNX更快？）

# ONNX 执行后端：cpu | cuda | directml | rocm
ONNX_EXECUTION_PROVIDER = os.environ.get("DICTATION_ONNX_PROVIDER", "cpu").strip().lower() or "cpu"
MAX_MATCH_DISTANCE = 400          # 射线法最大筛选距离 (t_hit 上限，像素)

# 选词同档容差：t_hit 差值在此像素内的字符视为同档，启用垂直距离二级排序
WORD_MATCH_TIE_TOLERANCE = 5   # 像素

# 保留：中文汉字、数字、大小写英文（排除空格、标点、特殊符号）
KEEP_CHAR = re.compile(r'[\u4e00-\u9fff\u0041-\u005a\u0061-\u007a]')

# 1.6 前向 ROI 模式
#   1 = 固定像素, 2 = 手部比例(×腕→食指MCP距离), 3 = 帧比例(×画面短边)
ROI_MODE = 2
# 是否对选框外区域涂黑（True=涂黑过滤，False=保留全矩形）
ROI_USE_MASK = True

# OCR 检测与识别参数
OCR_CONFIDENCE_THRESHOLD = 0.6    # OCR 结果置信度最低阈值
DET_THRESH = 0.25                  # 文字检测像素阈值（默认 0.3，降低到 0.25 减少漏检）
RETRIGGER_MOTION_RATIO = 0.4      # 指尖区域动检重触发阈值（0-1，越大越不灵敏，-1则不开启）
RETRIGGER_SHORT_SIDE_RATIO = 0.45   # 重触发距离阈值（ROI 短边的比例，0.5=短边的一半）

# 1.10 指尖 OCR 识别区域（相对原图的比例区间，0~1）
# 指尖落在该区域内才允许触发 OCR，否则跳过指向处理
OCR_AREA_X = (0.15, 0.85)   # 宽比例区间 [x_min, x_max]
OCR_AREA_Y = (0, 0.5)     # 高比例区间 [y_min, y_max]

# OCR_AREA 是否真正影响识别： True=指尖必须在区域内才触发 OCR（默认）； False=只用于显示标注/裁剪，实际全图识别
OCR_AREA_ENFORCE = False

# 前端摄像头画面显示模式："crop"=只显示 OCR 有效区域（裁剪推流，默认）；"full"=显示整个画面并在前端标注识别区域框
CAMERA_DISPLAY_MODE = "crop"

PREPROCESS_BRIGHTNESS_THRESHOLD = 110  # 预处理亮度阈值，ROI 平均亮度低于此值才进行 CLAHE+锐化（0-255）

# Mode 1: 固定像素值
ROI1_FORWARD = 90
ROI1_BACKWARD = 10
ROI1_SIDE = 50

# Mode 2: 手部关键点距离比例
ROI2_FORWARD_RATIO = 0.3
ROI2_BACKWARD_RATIO = 0.0
ROI2_SIDE_RATIO = 0.3

# Mode 3: 画面分辨率比例
ROI3_FORWARD_RATIO = 0.095
ROI3_BACKWARD_RATIO = 0.005
ROI3_SIDE_RATIO = 0.05

# 1.7 Pipeline 控制
FULL_PIPELINE = False              # False=仅检测+识别; True=含文档方向/展平/行方向

# 即时确认音开关：进入 TRIGGERED 状态时播放短提示音
ENABLE_CONFIRMATION_BEEP = False

# 1.8 丢帧处理配置
FRAME_SKIP_AFTER_OCR = 5         # OCR 后跳过多少帧的手部处理 (防止帧堆积导致延迟)

# 1.9 调试显示
SHOW_OCR_INPUT = True             # 左下角显示送入 OCR 的图像缩略图
SHOW_CAMERA_WINDOW = False  # True=OpenCV 弹窗显示摄像头画面； False=不弹窗（前端等待页显示画面）

# 定义模型本地路径（集中管理）
MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "models")

# PaddleX OCR 模型
PADDLEX_MODEL_DIR = os.path.join(MODELS_DIR, "paddlex_models")
PADDLEX_DET_DIR = os.path.join(PADDLEX_MODEL_DIR, "PP-OCRv6_medium_det")
PADDLEX_REC_DIR = os.path.join(PADDLEX_MODEL_DIR, "PP-OCRv6_medium_rec")

# ONNX 模型路径（与 Paddle 模型共享 inference.yml，在同一目录下）
PADDLEX_DET_ONNX = os.path.join(PADDLEX_DET_DIR, "model.onnx")
PADDLEX_REC_ONNX = os.path.join(PADDLEX_REC_DIR, "model.onnx")
PADDLEX_DOC_ORI_DIR = os.path.join(PADDLEX_MODEL_DIR, "PP-LCNet_x1_0_doc_ori")
PADDLEX_UNWARP_DIR = os.path.join(PADDLEX_MODEL_DIR, "UVDoc")
PADDLEX_TEXTLINE_ORI_DIR = os.path.join(PADDLEX_MODEL_DIR, "PP-LCNet_x1_0_textline_ori")

# MediaPipe 手部模型
HAND_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    "hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task"
)
HAND_MODEL_PATH = os.path.join(MODELS_DIR, "hand_landmarker.task")

# 1.10 词典与词库数据路径
CHINESE_DB_PATH = "data/chinese.db"     # 中文词典（来源于 pwxcoo/chinese-xinhua）
ENGLISH_DB_PATH = "data/english.db"     # 英汉词典（来源于 skywind3000/ECDICT）
STROKE_DATA_DIR = "data/strokes"         # 笔顺 JSON 文件（来源于 skishore/makemeahanzi）
JIEBA_DICT_PATH = "data/jieba_dict.txt" # jieba 分词词典

# 1.11 Web 服务器
WEB_HOST = "127.0.0.1"
WEB_PORT = 5002

# 是否显示平板下方的"返回主屏"按钮（所有页面常驻；False 则隐藏）
SHOW_BACK_BTN = False

# 主页链接（"返回主页"/"返回主屏"按钮跳转目标；留空则仅在 URL 带 ?back= 时跳转）
HOME_PAGE_URL = "http://127.0.0.1:8765/"

# 加载中文字体（遍历候选路径，自动选择第一个可用的）
FONT_CANDIDATES = [
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]

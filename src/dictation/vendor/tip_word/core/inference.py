"""
OCR 推理适配层（ONNX Runtime CPU 优化版）

封装 ONNX Runtime 推理 TextDetection + TextRecognition 为独立接口，
中间插入行级筛选，消除多余文字行的识别开销。

端侧移植时只需替换此文件中各适配器的实现（推理后端换掉），
上层 demo.py 的业务逻辑（ROI/CLAHE/筛选）不变。
"""

from loguru import logger

import math
import string
import cv2
import numpy as np
import os
import yaml

# ONNX 推理后端映射
_PROVIDER_MAP = {
    "cpu":      ["CPUExecutionProvider"],
    "cuda":     ["CUDAExecutionProvider", "CPUExecutionProvider"],
    "directml": ["DmlExecutionProvider", "CPUExecutionProvider"],
    "rocm":     ["ROCMExecutionProvider", "CPUExecutionProvider"],
}

# 提前导入，避免首次 OCR 时的延迟
try:
    from paddlex.inference.pipelines.components import cal_ocr_word_box
except ImportError:
    cal_ocr_word_box = None


# ==================== 文本检测适配器（ONNX Runtime）====================

class TextDetector:
    """文本检测适配器（DBNet）

    输入：BGR 图像 (H, W, 3)
    输出：文字行检测框列表，每个框为 np.ndarray shape (4, 2)，像素坐标

    推理后端：
    - "paddle": 使用 PaddleOCR 原始实现（PaddlePaddle 3.x）
    - "onnx":  使用 ONNX Runtime CPU 推理
    - "onnx_quant": 使用量化 ONNX 模型（model_quant.onnx）推理
    """

    def __init__(self, model_dir: str, thresh: float = 0.3, backend: str = "onnx"):
        self._backend = backend

        if backend == "paddle":
            from paddleocr import TextDetection
            self._det = TextDetection(
                model_name="PP-OCRv6_medium_det",
                model_dir=model_dir,
                thresh=thresh,
                enable_mkldnn=False,
            )
        elif backend in ("onnx", "onnx_quant"):
            import onnxruntime as ort
            onnx_path = os.path.join(model_dir, "model.onnx")
            if backend == "onnx_quant":
                quant_path = os.path.join(model_dir, "model_quant.onnx")
                if os.path.exists(quant_path):
                    onnx_path = quant_path
            if not os.path.exists(onnx_path):
                raise FileNotFoundError(f"ONNX 模型不存在: {onnx_path}")
            from configs import ONNX_EXECUTION_PROVIDER
            sess_opt = ort.SessionOptions()
            _ort_threads = int(os.environ.get("DICTATION_ORT_INTRA_THREADS", "0") or "0")
            if _ort_threads > 0:
                sess_opt.intra_op_num_threads = _ort_threads
            sess_opt.inter_op_num_threads = 1
            sess_opt.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self._session = ort.InferenceSession(
                onnx_path, sess_opt,
                providers=_PROVIDER_MAP.get(ONNX_EXECUTION_PROVIDER, ["CPUExecutionProvider"]),
            )
            # 检查输入名称
            self._input_name = self._session.get_inputs()[0].name
            self._output_name = self._session.get_outputs()[0].name

            # 预处理参数（来自 inference.yml NormalizeImage）
            self._mean = np.array([0.485, 0.456, 0.406], dtype=np.float32).reshape(1, 1, 3)
            self._std = np.array([0.229, 0.224, 0.225], dtype=np.float32).reshape(1, 1, 3)
            self._scale = 1.0 / 255.0

            # 从 inference.yml 读取后处理参数
            yml_path = os.path.join(model_dir, "inference.yml")
            if os.path.exists(yml_path):
                with open(yml_path, 'r', encoding='utf-8') as f:
                    cfg = yaml.safe_load(f)
                post = cfg.get('PostProcess', {})
                self._db_thresh = post.get('thresh', 0.2)
                self._box_thresh = post.get('box_thresh', 0.45)
                self._unclip_ratio = post.get('unclip_ratio', 1.4)
                self._max_candidates = post.get('max_candidates', 3000)
            else:
                self._db_thresh = 0.2
                self._box_thresh = 0.45
                self._unclip_ratio = 1.4
                self._max_candidates = 3000

            # resize 参数（DetResizeForTest 默认值：max edge > limit 时缩小）
            self._limit_side_len = 960
            self._limit_type = "max"
            self._max_side_limit = 4000
        else:
            raise ValueError(f"未知的 OCR 推理后端: {backend}")

    def _resize_image(self, image):
        """DetResizeForTest 风格 resize：按 limit_side_len 缩放，short edge 策略"""
        h, w = image.shape[:2]

        # 选择缩放比例
        if self._limit_type == "min":
            if min(h, w) < self._limit_side_len:
                ratio = float(self._limit_side_len) / min(h, w)
            else:
                ratio = 1.0
        elif self._limit_type == "max":
            if max(h, w) > self._limit_side_len:
                ratio = float(self._limit_side_len) / max(h, w)
            else:
                ratio = 1.0
        else:  # resize_long
            ratio = float(self._limit_side_len) / max(h, w)

        resize_h = int(h * ratio)
        resize_w = int(w * ratio)

        # 限制最大边长
        if max(resize_h, resize_w) > self._max_side_limit:
            ratio = float(self._max_side_limit) / max(resize_h, resize_w)
            resize_h = int(resize_h * ratio)
            resize_w = int(resize_w * ratio)

        # 对齐到 32 的倍数（用 resize 拉伸，与 PaddleOCR 一致）
        resize_h = max(int(round(resize_h / 32) * 32), 32)
        resize_w = max(int(round(resize_w / 32) * 32), 32)

        if resize_h != h or resize_w != w:
            image = cv2.resize(image, (resize_w, resize_h))

        ratio_h = resize_h / float(h)
        ratio_w = resize_w / float(w)
        return image, (h, w), (ratio_h, ratio_w)

    def _preprocess(self, image):
        """预处理：resize → NormalizeImage → HWC→CHW → NCHW"""
        img, src_shape, (ratio_h, ratio_w) = self._resize_image(image)

        # NormalizeImage
        img = img.astype(np.float32) * self._scale
        img = (img - self._mean) / self._std

        # HWC -> CHW -> NCHW
        img = np.transpose(img, (2, 0, 1))[np.newaxis, ...]
        return img.astype(np.float32), src_shape, (ratio_h, ratio_w)

    def _postprocess(self, pred, src_shape, ratios):
        """DB 后处理（与 PaddleX DBPostProcess 对齐）

        Args:
            pred: 模型输出的概率图 (1, 1, H, W)
            src_shape: 原始图像尺寸 (h, w)
            ratios: (ratio_h, ratio_w)
        """
        h_src, w_src = src_shape
        ratio_h, ratio_w = ratios

        bitmap = pred[0, 0]  # 模型已含 sigmoid，直接在 [0,1] 范围
        h_pred, w_pred = bitmap.shape

        logger.debug(f"[ONNX-DET] pred shape={pred.shape} min={bitmap.min():.4f} max={bitmap.max():.4f}")
        if bitmap.max() < self._db_thresh:
            logger.warning(f"[ONNX-DET] 概率图最大值 {bitmap.max():.4f} 低于阈值 {self._db_thresh}，无检出")

        # 二值化
        mask = (bitmap > self._db_thresh).astype(np.uint8)

        # 找轮廓
        contours, _ = cv2.findContours(
            (mask * 255).astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
        )

        boxes = []
        n_contours = len(contours)
        n_skipped_small = 0
        n_skipped_unclip = 0
        n_skipped_mini = 0
        n_skipped_score = 0
        for contour in contours[:self._max_candidates]:
            # 用 minAreaRect 获取紧贴矩形（与 PaddleOCR 一致）
            rect = cv2.minAreaRect(contour)
            points = np.int32(cv2.boxPoints(rect))
            if points.shape[0] < 4:
                n_skipped_small += 1
                continue

            # ★ 在 unclip 之前评分（与 PaddleOCR boxes_from_bitmap 顺序一致）
            score = self._box_score_fast(bitmap, points.reshape(-1, 2))
            if score < self._box_thresh:
                n_skipped_score += 1
                continue

            # unclip: 膨胀轮廓
            area = cv2.contourArea(points)
            length = cv2.arcLength(points, True)
            distance = area * self._unclip_ratio / length if length > 0 else 0
            try:
                import pyclipper
                offset = pyclipper.PyclipperOffset()
                offset.AddPath(points, pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
                expanded = np.array(offset.Execute(distance))
                if len(expanded) == 0:
                    n_skipped_unclip += 1
                    continue
                box = expanded.reshape(-1, 2)
            except (ImportError, ValueError):
                # 无 pyclipper 时 fallback 到 minAreaRect
                rect2 = cv2.minAreaRect(points)
                box = cv2.boxPoints(rect2)

            if len(box) < 4:
                continue

            # 获取 mini 包围盒（4 点有序）
            box, sside = self._get_mini_boxes(box)
            if sside < 5:  # min_size + 2
                n_skipped_mini += 1
                continue

            # 缩放到原始图像尺寸
            box = box.astype(np.float32)
            for i in range(box.shape[0]):
                box[i, 0] = max(0, min(round(box[i, 0] / ratio_w), w_src))
                box[i, 1] = max(0, min(round(box[i, 1] / ratio_h), h_src))

            boxes.append(box.astype(np.int32))

        logger.debug(
            f"[ONNX-DET] contours={n_contours} boxes={len(boxes)} "
            f"skip(small={n_skipped_small} unclip={n_skipped_unclip} "
            f"mini={n_skipped_mini} score={n_skipped_score})"
        )
        return boxes

    def _get_mini_boxes(self, contour):
        """获取 4 点有序包围盒"""
        rect = cv2.minAreaRect(contour.astype(np.float32))
        points = sorted(list(cv2.boxPoints(rect)), key=lambda x: x[0])

        index_1, index_2, index_3, index_4 = 0, 1, 2, 3
        if points[1][1] > points[0][1]:
            index_1 = 0
            index_4 = 1
        else:
            index_1 = 1
            index_4 = 0
        if points[3][1] > points[2][1]:
            index_2 = 2
            index_3 = 3
        else:
            index_2 = 3
            index_3 = 2

        box = [points[index_1], points[index_2], points[index_3], points[index_4]]
        return np.array(box), min(rect[1])

    def _box_score_fast(self, bitmap, _box):
        """计算框内平均概率得分"""
        h, w = bitmap.shape[:2]
        box = _box.copy()
        xmin = max(0, min(math.floor(box[:, 0].min()), w - 1))
        xmax = max(0, min(math.ceil(box[:, 0].max()), w - 1))
        ymin = max(0, min(math.floor(box[:, 1].min()), h - 1))
        ymax = max(0, min(math.ceil(box[:, 1].max()), h - 1))

        if xmax <= xmin or ymax <= ymin:
            return 0.0

        mask = np.zeros((ymax - ymin + 1, xmax - xmin + 1), dtype=np.uint8)
        box[:, 0] = box[:, 0] - xmin
        box[:, 1] = box[:, 1] - ymin
        cv2.fillPoly(mask, box.reshape(1, -1, 2).astype(np.int32), 1)
        return cv2.mean(bitmap[ymin:ymax + 1, xmin:xmax + 1], mask)[0]

    def predict(self, image: np.ndarray):
        """检测图像中的文字行，返回 dt_polys 列表

        Returns:
            list[np.ndarray(4,2)]: 每个元素是一个文字行的 4 个角点
        """
        if self._backend == "paddle":
            results = list(self._det.predict(image))
            if not results:
                return []
            polys = results[0]["dt_polys"]  # shape (N, 4, 2), dtype np.int16
            if polys is None or len(polys) == 0:
                return []
            return [polys[i] for i in range(polys.shape[0])]
        else:
            img_tensor, src_shape, ratios = self._preprocess(image)
            outputs = self._session.run([self._output_name], {self._input_name: img_tensor})
            boxes = self._postprocess(outputs[0], src_shape, ratios)
            return boxes

    def close(self):
        """释放检测模型资源"""
        if self._backend == "paddle":
            if hasattr(self, '_det') and hasattr(self._det, 'close'):
                self._det.close()


# ==================== 文本识别适配器（ONNX Runtime）====================

class TextRecognizer:
    """文本识别适配器（CRNN）

    输入：裁剪后的文本行图像列表 [(H1,W1,3), ...] BGR
    输出：dict with keys rec_text, rec_score

    推理后端：
    - "paddle": 使用 PaddleOCR 原始实现（PaddlePaddle 3.x）
    - "onnx":  使用 ONNX Runtime CPU 推理
    - "onnx_quant": 使用量化 ONNX 模型（model_quant.onnx）推理
    """

    def __init__(self, model_dir: str, backend: str = "onnx"):
        self._backend = backend

        if backend == "paddle":
            from paddleocr import TextRecognition
            self._rec = TextRecognition(
                model_name="PP-OCRv6_medium_rec",
                model_dir=model_dir,
                enable_mkldnn=False,
            )
        elif backend in ("onnx", "onnx_quant"):
            import onnxruntime as ort
            onnx_path = os.path.join(model_dir, "model.onnx")
            if backend == "onnx_quant":
                quant_path = os.path.join(model_dir, "model_quant.onnx")
                if os.path.exists(quant_path):
                    onnx_path = quant_path
            if not os.path.exists(onnx_path):
                raise FileNotFoundError(f"ONNX 模型不存在: {onnx_path}")
            sess_opt = ort.SessionOptions()
            _ort_threads = int(os.environ.get("DICTATION_ORT_INTRA_THREADS", "0") or "0")
            if _ort_threads > 0:
                sess_opt.intra_op_num_threads = _ort_threads
            sess_opt.inter_op_num_threads = 1
            sess_opt.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            from configs import ONNX_EXECUTION_PROVIDER
            self._session = ort.InferenceSession(
                onnx_path, sess_opt, providers=_PROVIDER_MAP.get(ONNX_EXECUTION_PROVIDER, ["CPUExecutionProvider"])
            )
            self._input_name = self._session.get_inputs()[0].name
            self._output_name = self._session.get_outputs()[0].name

            # 预处理参数
            self._mean = np.array([0.485, 0.456, 0.406], dtype=np.float32).reshape(1, 1, 3)
            self._std = np.array([0.229, 0.224, 0.225], dtype=np.float32).reshape(1, 1, 3)
            self._scale = 1.0 / 255.0
            self._rec_height = 48
            self._rec_width = 320

            # 从 inference.yml 读取词汇表，构建与 PaddleOCR 一致的字符映射
            yml_path = os.path.join(model_dir, "inference.yml")
            if os.path.exists(yml_path):
                with open(yml_path, 'r', encoding='utf-8') as f:
                    cfg = yaml.safe_load(f)
                char_dict = cfg.get('PostProcess', {}).get('character_dict', [])
            else:
                raise FileNotFoundError(f"找不到推理配置文件: {yml_path}")

            # 构建完整字符表：["blank"] + char_dict + [" "]
            # 与 PaddleOCR CTCLabelDecode.add_special_char + use_space_char 一致
            self._vocab = ["blank"] + list(char_dict) + [" "]
            self._blank_idx = 0
            self._num_classes = len(self._vocab)
            # 不再使用固定目标宽度，_preprocess 中按实际图像动态计算
        else:
            raise ValueError(f"未知的 OCR 推理后端: {backend}")

    def _preprocess(self, image):
        """预处理：resize 高度到 48，保持宽高比，pad 或裁剪到 320 宽度"""
        h, w = image.shape[:2]
        ratio = self._rec_height / h
        new_w = int(w * ratio)

        # resize 高度到 48，保持宽高比（与 PaddleOCR 一致：几乎零 padding）
        image = cv2.resize(image, (new_w, self._rec_height))

        # 归一化：(x/255 - 0.5)/0.5 → 范围 [-1, 1]（PP-OCRv6 训练时的预处理）
        img = image.astype(np.float32) / 255.0
        img = (img - 0.5) / 0.5

        # HWC -> CHW -> NCHW
        img = np.transpose(img, (2, 0, 1))[np.newaxis, ...]
        return img.astype(np.float32)

    def _postprocess(self, output):
        """CTC 贪心解码（与 PaddleOCR CTCLabelDecode.decode 一致）

        Args:
            output: 模型输出 [1, T, num_classes]

        Returns:
            (text_string, confidence)
        """
        pred = np.array(output[0])  # [1, T, C] or [T, C]
        if pred.ndim == 3:
            pred = pred[0]  # [T, C]

        # argmax
        pred_idx = pred.argmax(axis=-1)  # [T]
        pred_prob = pred.max(axis=-1)    # [T]

        # 去除空白符和连续重复
        selection = np.ones(len(pred_idx), dtype=bool)
        selection[1:] = pred_idx[1:] != pred_idx[:-1]
        selection &= pred_idx != self._blank_idx

        char_ids = pred_idx[selection]
        if len(char_ids) == 0:
            return "", 0.0, None, []

        # 转字符
        chars = [self._vocab[cid] for cid in char_ids]
        text = "".join(chars)

        # 置信度：所有非 blank 位置的平均概率
        conf = float(np.mean(pred_prob[selection])) if selection.any() else 0.0

        # 构建 word_info：用于 cal_ocr_word_box 精确计算逐字框
        selection_indices = np.where(selection)[0]  # 非 blank 非重复位置的时间步索引
        word_info = self._build_word_info(chars, selection_indices, pred_idx, pred_prob)

        # Keep each decoded character's actual CTC activation run.  The blank
        # columns between adjacent runs are valuable visual-boundary evidence
        # for Chinese vocabulary tables and are lost by ordinary CTC decode.
        char_spans = []
        for char, start in zip(chars, selection_indices):
            start = int(start)
            label = int(pred_idx[start])
            end = start + 1
            while end < len(pred_idx) and int(pred_idx[end]) == label:
                end += 1
            run_conf = float(np.mean(pred_prob[start:end])) if end > start else 0.0
            char_spans.append({
                "char": char,
                "start": start,
                "end": end,
                "center": (start + end - 1) / 2.0,
                "col_num": int(len(pred_idx)),
                "confidence": run_conf,
            })

        return text, conf, word_info, char_spans

    def _build_word_info(self, chars, sel_indices, pred_idx, pred_prob):
        """从 CTC 输出构建 word_info (col_num, word_list, word_col_list, state_list)"""
        col_num = len(pred_idx)
        word_list = []
        word_col_list = []
        state_list = []

        if len(chars) == 0:
            return [col_num, word_list, word_col_list, state_list]

        i = 0
        while i < len(chars):
            ch = chars[i]
            col = int(sel_indices[i])

            # 判断是否为 CJK 字符（中文等）
            is_cjk = '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf'
            state = "cn" if is_cjk else "en"

            if is_cjk:
                # 中文：每字单独一个框
                word_list.append([ch])
                word_col_list.append([col])
                state_list.append(state)
                i += 1
            else:
                # 英文/数字：按空格拆分单词
                group_chars = []
                group_cols = []
                while i < len(chars):
                    ci = chars[i]
                    if '\u4e00' <= ci <= '\u9fff' or '\u3400' <= ci <= '\u4dbf':
                        break
                    if ci == ' ':
                        if group_chars:
                            word_list.append(group_chars)
                            word_col_list.append(group_cols)
                            state_list.append("en")
                        group_chars, group_cols = [], []
                        i += 1
                        continue
                    group_chars.append(ci)
                    group_cols.append(int(sel_indices[i]))
                    i += 1
                if group_chars:
                    word_list.append(group_chars)
                    word_col_list.append(group_cols)
                    state_list.append("en")

        return [col_num, word_list, word_col_list, state_list]

    def predict(self, cropped_list: list[np.ndarray], return_word_box=False):
        """识别裁剪后的文本行列表

        Args:
            cropped_list: BGR 图像列表
            return_word_box: 是否返回 word_info（用于 cal_ocr_word_box）

        Returns:
            dict with keys:
                "rec_text": list[str] | str — 识别文本
                "rec_score": list[float] | float — 置信度
        """
        if self._backend == "paddle":
            results = list(self._rec.predict(cropped_list, return_word_box=return_word_box))
            if not results:
                return None
            return results[0]

        texts = []
        scores = []
        texts_with_info = []  # 当 return_word_box=True 时使用
        char_spans_list = []

        for img in cropped_list:
            img_tensor = self._preprocess(img)
            outputs = self._session.run(
                [self._output_name], {self._input_name: img_tensor}
            )
            text, conf, word_info, char_spans = self._postprocess(outputs)

            texts.append(text)
            scores.append(conf)
            char_spans_list.append(char_spans)

            if return_word_box:
                texts_with_info.append((text, word_info))

        # 构建与原始 PaddleOCR 兼容的返回值
        if return_word_box:
            # 当 return_word_box=True 时，rec_text 包含 (str, word_info) 元组
            result_text = texts_with_info if len(texts_with_info) > 1 else (
                texts_with_info[0] if texts_with_info else ''
            )
        else:
            result_text = texts if len(texts) > 1 else (
                texts[0] if len(texts) == 1 else ''
            )

        result_score = scores if len(scores) > 1 else (
            scores[0] if len(scores) == 1 else 0.0
        )

        response = {
            'rec_text': result_text,
            'rec_score': result_score,
        }
        if return_word_box:
            response['rec_char_spans'] = (
                char_spans_list if len(char_spans_list) > 1 else
                (char_spans_list[0] if char_spans_list else [])
            )
        return response

    def close(self):
        """释放识别模型资源"""
        if self._backend == "paddle":
            if hasattr(self, '_rec') and hasattr(self._rec, 'close'):
                self._rec.close()


# ==================== 工具函数 ====================

def get_rotate_crop_image(img: np.ndarray, points: np.ndarray) -> np.ndarray:
    """透视变换裁剪文本行，校正倾斜

    Args:
        img: 原始图像 (H, W, 3) BGR
        points: 4×2 检测框坐标（左上-右上-右下-左下）

    Returns:
        校正后的文本行图像 (H', W', 3) BGR
    """
    assert len(points) == 4, f"points must be 4×2, got {points.shape}"

    # 计算目标宽度（取上下边的较大值）
    img_crop_width = int(max(
        np.linalg.norm(points[0] - points[1]),
        np.linalg.norm(points[2] - points[3]),
    ))
    # 计算目标高度（取左右边的较大值）
    img_crop_height = int(max(
        np.linalg.norm(points[0] - points[3]),
        np.linalg.norm(points[1] - points[2]),
    ))
    if img_crop_width < 1 or img_crop_height < 1:
        return img

    pts_std = np.float32([
        [0, 0],
        [img_crop_width, 0],
        [img_crop_width, img_crop_height],
        [0, img_crop_height],
    ])
    pts = points.astype(np.float32)
    M = cv2.getPerspectiveTransform(pts, pts_std)
    dst_img = cv2.warpPerspective(
        img, M, (img_crop_width, img_crop_height),
        borderMode=cv2.BORDER_REPLICATE,
        flags=cv2.INTER_CUBIC,
    )

    # 高度远大于宽度时旋转（竖排文字处理）
    h, w = dst_img.shape[:2]
    if w > 0 and h / w >= 1.5:
        dst_img = np.rot90(dst_img)
    return dst_img


def _poly_to_obb(poly):
    """将 4 点检测框转为 OBB 参数 (cx, cy, half_w, half_h, cos_t, sin_t)"""
    cx = float(np.mean(poly[:, 0]))
    cy = float(np.mean(poly[:, 1]))
    # 取两条相邻边的中点距离作为半宽和半高
    e1 = np.linalg.norm(poly[1] - poly[0])
    e2 = np.linalg.norm(poly[2] - poly[1])
    half_w = max(e1, e2) / 2.0
    half_h = min(e1, e2) / 2.0
    # 用最长边的方向作为角度
    if e1 >= e2:
        dx = poly[1][0] - poly[0][0]
        dy = poly[1][1] - poly[0][1]
    else:
        dx = poly[2][0] - poly[1][0]
        dy = poly[2][1] - poly[1][1]
    edge_len = math.hypot(dx, dy)
    if edge_len > 0:
        cos_t = dx / edge_len
        sin_t = dy / edge_len
    else:
        cos_t, sin_t = 1.0, 0.0
    return cx, cy, half_w, half_h, cos_t, sin_t


def filter_target_text_line(dt_polys, tip_px, direction_vec, max_dist=400, ray_margin=15):
    """射线法：指尖发射带厚度射线，寻找被穿透的检测框

    步骤：
        1. 射线-OBB 相交检测，找 t_hit 最小的框
        2. 无相交时兜底：找框顶点到射线距离最小的框

    Returns:
        (target_poly, target_center) 或 (None, None)
    """
    if not dt_polys:
        return None, None

    tip_x, tip_y = tip_px
    dir_x, dir_y = direction_vec  # 已归一化

    intersected = []  # [(poly, center, t_hit), ...]

    # ---------- Step 1: 射线-OBB 相交检测 ----------
    for poly in dt_polys:
        cx, cy, half_w, half_h, cos_t, sin_t = _poly_to_obb(poly)

        # 变换到局部坐标系（将框中心移到原点，旋转使框与轴对齐）
        vx = tip_x - cx
        vy = tip_y - cy
        # 逆旋转：R(-θ)
        v_local_x = vx * cos_t + vy * sin_t
        v_local_y = -vx * sin_t + vy * cos_t
        d_local_x = dir_x * cos_t + dir_y * sin_t
        d_local_y = -dir_x * sin_t + dir_y * cos_t

        # 给射线厚度：Y 轴范围扩大
        effective_half_h = half_h + ray_margin

        # Slab 算法求 t_enter / t_leave
        # X 轴
        if abs(d_local_x) < 1e-8:
            if v_local_x < -half_w or v_local_x > half_w:
                continue
            t_min_x, t_max_x = -float('inf'), float('inf')
        else:
            t1 = (-half_w - v_local_x) / d_local_x
            t2 = (half_w - v_local_x) / d_local_x
            t_min_x, t_max_x = min(t1, t2), max(t1, t2)

        # Y 轴（带厚度 margin）
        if abs(d_local_y) < 1e-8:
            if v_local_y < -effective_half_h or v_local_y > effective_half_h:
                continue
            t_min_y, t_max_y = -float('inf'), float('inf')
        else:
            t1 = (-effective_half_h - v_local_y) / d_local_y
            t2 = (effective_half_h - v_local_y) / d_local_y
            t_min_y, t_max_y = min(t1, t2), max(t1, t2)

        t_enter = max(t_min_x, t_min_y)
        t_leave = min(t_max_x, t_max_y)

        if t_enter <= t_leave and t_leave >= 0:
            t_hit = max(0.0, t_enter)
            if t_hit <= max_dist:
                intersected.append((poly, (cx, cy), t_hit))

    # 按 t_hit 排序，取最近的
    if intersected:
        intersected.sort(key=lambda x: x[2])
        best = intersected[0]
        return best[0], best[1]

    # ---------- Step 2: 兜底：最近邻 ----------
    best_poly = None
    best_center = None
    best_dist = float('inf')

    for poly in dt_polys:
        cx = float(np.mean(poly[:, 0]))
        cy = float(np.mean(poly[:, 1]))
        vec_x = cx - tip_x
        vec_y = cy - tip_y
        # 必须在指尖前方
        if vec_x * dir_x + vec_y * dir_y <= 0:
            continue

        # 计算 4 个顶点到射线的最短距离
        min_vdist = float('inf')
        for k in range(4):
            px = poly[k, 0] - tip_x
            py = poly[k, 1] - tip_y
            cross = abs(px * dir_y - py * dir_x)  # 叉积 = 垂直距离
            fwd = px * dir_x + py * dir_y
            if fwd > 0 and cross < min_vdist:
                min_vdist = cross

        if min_vdist < best_dist:
            best_dist = min_vdist
            best_poly = poly
            best_center = (cx, cy)

    if best_dist <= max_dist:
        return best_poly, best_center
    return None, None


def build_char_infos(text, score, dt_poly):
    """等分 dt_poly 包围盒构建逐字坐标（兼容旧格式的 fallback）"""
    if not text:
        return []
    xs = dt_poly[:, 0]
    ys = dt_poly[:, 1]
    min_x, max_x = float(np.min(xs)), float(np.max(xs))
    min_y, max_y = float(np.min(ys)), float(np.max(ys))
    char_w = (max_x - min_x) / len(text)
    chars = []
    for j, ch in enumerate(text):
        box = [
            [min_x + j * char_w, min_y],
            [min_x + (j + 1) * char_w, min_y],
            [min_x + (j + 1) * char_w, max_y],
            [min_x + j * char_w, max_y],
        ]
        chars.append({'char': ch, 'box': box})
    return [{
        'text': text,
        'conf': score,
        'dt_box': dt_poly.tolist(),
        'chars': chars,
    }]


def build_word_box_char_infos(rec_text_output, rec_score, dt_poly):
    """利用 cal_ocr_word_box 构建精确词级/字级坐标

    - 英文：整词一个框（如 "strawberry" 整体 → 1 个 chars 项）
    - 中文：逐字成框（如 "你好" → 2 个 chars 项）
    - 混合：中文逐字 + 英文整词

    Args:
        rec_text_output: 识别结果，str 或 (str, word_info) 元组
        rec_score: 置信度
        dt_poly: 检测框 np.ndarray(4,2)，全图坐标

    Returns:
        list[dict]: match_target_word 兼容的 char_info_list
    """
    # 解析识别结果格式
    text = None
    word_info = None
    if isinstance(rec_text_output, str):
        text = rec_text_output
    elif isinstance(rec_text_output, (tuple, list)) and len(rec_text_output) >= 2:
        text = rec_text_output[0]
        word_info = rec_text_output[1]

    if not text:
        return []

    # 有 word_info → 用 cal_ocr_word_box 精确计算框
    if word_info is not None and cal_ocr_word_box is not None:
        try:
            content_list, box_list = cal_ocr_word_box(text, dt_poly, word_info)
            chars = []
            for content, box_pts in zip(content_list, box_list):
                # box_pts: ((x1,y1), (x2,y2), (x3,y3), (x4,y4))
                box = [[p[0], p[1]] for p in box_pts]
                chars.append({'char': content, 'box': box})
            return [{
                'text': text,
                'conf': rec_score,
                'dt_box': dt_poly.tolist() if hasattr(dt_poly, 'tolist') else dt_poly,
                'chars': chars,
            }]
        except Exception as e:
            logger.warning(f"cal_ocr_word_box 失败 ({e})，fallback 等分")

    # fallback：等分 dt_poly
    return build_char_infos(text, rec_score, dt_poly)


def get_polygon_center(poly):
    """计算多边形中心点"""
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return (sum(xs) / len(poly), sum(ys) / len(poly))

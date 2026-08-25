"""核心算法模块，通过 from core import * 统一导出"""

from .motion import MotionDetector
from .hand_tracker import HandTracker, ensure_hand_model
from .smoother import CoordinateSmoother
from .roi import (
    compute_roi_corners,
    extract_rotated_roi,
    compute_hand_ref_dist,
    compute_retrigger_threshold,
)
from .matcher import match_target_word, SpatialMatcher, get_polygon_center, get_polygon_bounds
from .ocr_utils import extract_ocr_char_info
from .inference import (
    TextDetector, TextRecognizer,
    filter_target_text_line, get_rotate_crop_image,
    build_char_infos, build_word_box_char_infos,
)
from .visualize import cv2_put_chinese_text, draw_ocr_area

__all__ = [
    "MotionDetector",
    "HandTracker",
    "ensure_hand_model",
    "CoordinateSmoother",
    "compute_roi_corners",
    "extract_rotated_roi",
    "compute_hand_ref_dist",
    "compute_retrigger_threshold",
    "match_target_word",
    "SpatialMatcher",
    "get_polygon_center",
    "get_polygon_bounds",
    "extract_ocr_char_info",
    "TextDetector",
    "TextRecognizer",
    "filter_target_text_line",
    "get_rotate_crop_image",
    "build_char_infos",
    "build_word_box_char_infos",
    "cv2_put_chinese_text",
    "draw_ocr_area",
]

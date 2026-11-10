# -*- coding: utf-8 -*-
"""OCR 层测试：不依赖模型也能跑（没有 venv-ocr 时自动跳过真实识别）。

覆盖三件事：
1. 客户端在服务缺失/异常时**不抛异常、不阻塞**，只返回空结果；
2. 真实截图的识别结果（有模型时）——「钓具准备好」这类关键判据；
3. ROI 越界、空帧、坏图等边界不崩。
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:                                    # 系统 Python 没有 cv2/numpy：整套跳过
    import cv2
    import numpy as np
except ModuleNotFoundError:             # pragma: no cover - 环境相关
    raise unittest.SkipTest("当前解释器没有 cv2/numpy（请用 bot 的 venv 跑）")

import autofish_ocr_client as oc  # noqa: E402

MODEL_DIR = os.path.join(HERE, "models", "ocr")
HAS_MODELS = os.path.isdir(MODEL_DIR) and any(
    n.startswith("PP-OCR") for n in os.listdir(MODEL_DIR)
)
HAS_VENV = os.path.exists(oc.OCR_PYTHON)
SCREENSHOT = os.path.join(HERE, "_fullflow_fault.png")


class ClientFallbackTests(unittest.TestCase):
    """服务不可用时的兜底：调用方永远拿到空结果，而不是异常。"""

    def test_missing_python_reports_unavailable(self):
        client = oc.OcrClient(python_exe=os.path.join(HERE, "no_such_python.exe"))
        self.assertFalse(client.available)
        frame = np.zeros((40, 80, 3), np.uint8)
        self.assertEqual(client.read_line(frame), ("", 0.0))
        self.assertEqual(client.read_block(frame), [])
        client.close()

    def test_missing_service_script_reports_unavailable(self):
        client = oc.OcrClient(service=os.path.join(HERE, "no_such_service.py"))
        self.assertFalse(client.available)
        self.assertEqual(client.read_block_text(None), "")
        client.close()

    def test_none_frame_is_empty_not_crash(self):
        client = oc.OcrClient()
        self.assertEqual(client.read_line(None), ("", 0.0))
        client.close()

    def test_out_of_range_roi_is_empty(self):
        client = oc.OcrClient()
        frame = np.zeros((50, 50, 3), np.uint8)
        self.assertEqual(client.read_line(frame, roi=(500, 500, 40, 20)), ("", 0.0))
        client.close()

    def test_close_is_idempotent(self):
        client = oc.OcrClient()
        client.close()
        client.close()


@unittest.skipUnless(HAS_MODELS and HAS_VENV, "缺少 venv-ocr 或 OCR 模型")
class RealRecognitionTests(unittest.TestCase):
    """有模型时的真实识别（离线截图，不抓屏、不发输入）。"""

    @classmethod
    def setUpClass(cls):
        cls.client = oc.OcrClient()
        if not cls.client.available:
            raise unittest.SkipTest("OCR 服务不可用")

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_ready_prompt_on_real_screenshot(self):
        """待抛竿提示必须读得出来——它是抛竿判据，比模板匹配可靠。"""
        if not os.path.exists(SCREENSHOT):
            self.skipTest("缺少离线截图")
        img = cv2.imread(SCREENSHOT)
        if img is None:
            self.skipTest("离线截图不可读")
        text, score = self.client.read_line(img, (532, 1014, 200, 32))
        self.assertIn("钓具准备好", text.replace(" ", ""))
        self.assertGreater(score, 0.8)

    def test_digits_on_synthetic_image(self):
        """数字读数：合成一张白底黑字，验证不会把数字读丢。"""
        img = np.full((60, 200, 3), 255, np.uint8)
        cv2.putText(img, "17", (10, 45), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 3)
        text, score = self.client.read_line(img)
        self.assertIn("17", text)

    def test_blank_image_reads_nothing(self):
        img = np.full((40, 120, 3), 128, np.uint8)
        text, score = self.client.read_line(img)
        self.assertEqual(text, "")


if __name__ == "__main__":
    unittest.main()

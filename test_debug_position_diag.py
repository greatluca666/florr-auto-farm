"""debug_position_diag.py 的冒烟测试: 把所有实机依赖(截屏/倒计时/真实get_map)打桩掉,
只验证它能跑完、该写的三个文件都写了, 不炸。诊断脚本本身不是给自动化测试判定"结论对不
对"用的(那是人在实机上肉眼看图), 这里只锁"代码路径能走通", 照 test_capture_map.py
的打桩风格.
"""
import numpy as np

import debug_position_diag as diag


def _fake_minimap_with_marker():
    """300x300, 左上角贴一小块玩家标记色, 让"match_count > 0"那条分支也被走到。"""
    img = np.zeros((300, 300, 3), dtype=np.uint8)
    img[10:14, 10:14] = (0x60, 0xde, 0xf8)   # f8de60 的 BGR 顺序
    return img


def test_main_runs_to_completion_and_writes_all_three_files(monkeypatch, tmp_path):
    monkeypatch.setattr(diag, "get_map", _fake_minimap_with_marker)
    monkeypatch.setattr(diag.pyautogui, "screenshot",
                        lambda: np.zeros((diag.SCREEN_HEIGHT, diag.SCREEN_WIDTH, 3), dtype=np.uint8))
    monkeypatch.setattr(diag.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.chdir(tmp_path)

    diag.main()   # 没有异常炸出来就是这条测试的第一道关

    for name in ("debug_position_diag_fullscreen.png",
                 "debug_position_diag_raw.png",
                 "debug_position_diag_marked.png"):
        assert (tmp_path / name).exists(), f"{name} 没写出来"


def test_main_skips_marked_png_when_nothing_matches(monkeypatch, tmp_path):
    # match_count==0 那条分支不聚连通块, 不该尝试写 debug_position_diag_marked.png ——
    # 这条锁住"0匹配时代码走的是哪条分支", 不是泛泛地"跑完就行".
    monkeypatch.setattr(diag, "get_map", lambda: np.zeros((300, 300, 3), dtype=np.uint8))
    monkeypatch.setattr(diag.pyautogui, "screenshot",
                        lambda: np.zeros((diag.SCREEN_HEIGHT, diag.SCREEN_WIDTH, 3), dtype=np.uint8))
    monkeypatch.setattr(diag.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.chdir(tmp_path)

    diag.main()

    assert (tmp_path / "debug_position_diag_fullscreen.png").exists()
    assert (tmp_path / "debug_position_diag_raw.png").exists()
    assert not (tmp_path / "debug_position_diag_marked.png").exists()


def test_fullscreen_overlay_draws_the_capture_region_as_a_red_box(monkeypatch, tmp_path):
    # 红框到底画没画、画在哪——这是这次新加的诊断能力, 不能只测"文件存在".
    import cv2

    monkeypatch.setattr(diag, "get_map", lambda: np.zeros((300, 300, 3), dtype=np.uint8))
    monkeypatch.setattr(diag.pyautogui, "screenshot",
                        lambda: np.zeros((diag.SCREEN_HEIGHT, diag.SCREEN_WIDTH, 3), dtype=np.uint8))
    monkeypatch.setattr(diag.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.chdir(tmp_path)

    diag.main()

    region = diag.minimap_capture_region()
    fx, fy, fw, fh = region
    overlay = cv2.imread(str(tmp_path / "debug_position_diag_fullscreen.png"))
    assert overlay is not None
    # 矩形边框是纯红(BGR=(0,0,255)); 左上角那条边上必须能找到红色像素, 说明红框
    # 真画在了 region 算出来的位置上, 不是画在别处/根本没画.
    assert tuple(overlay[fy, fx + fw // 2]) == (0, 0, 255)

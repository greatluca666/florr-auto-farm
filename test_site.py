import re
from pathlib import Path

SITE = Path(__file__).resolve().parent / "site"

# 除了自己的仓库, 页面还会链到 florr-auto-afk(配套程序)和上游 fork 出处 —— 都是公开
# README 已经写明的关系, 不算"多余的对外声明".
_ALLOWED_LINK_PREFIXES = (
    "https://github.com/greatluca666/florr-auto-farm",
    "https://github.com/greatluca666/florr-auto-afk",
    "https://github.com/Shiny-Ladybug/florr-auto-pathing",
)
_ALLOWED_STYLESHEET = (
    "https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700"
    "&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap"
)


def _html():
    return (SITE / "index.html").read_text(encoding="utf-8")


def _js():
    return (SITE / "app.js").read_text(encoding="utf-8")


def test_every_id_app_js_looks_up_exists_in_html():
    ids = set(re.findall(r'id="([^"]+)"', _html()))
    used = set(re.findall(r'\$\("([^"]+)"\)', _js()))
    used |= set(re.findall(r'querySelectorAll\("#([\w-]+)', _js()))
    assert used, 'app.js 应当通过 $("id") 或 "#id ..." 选择器取元素'
    assert used <= ids, used - ids


def test_local_assets_exist():
    for ref in re.findall(r'(?:href|src)="(/[^"#]*)"', _html()):
        if ref != "/":
            assert (SITE / ref.lstrip("/")).is_file(), ref


def test_external_anchor_links_are_only_the_known_repos():
    for ref in re.findall(r'<a\b[^>]*href="(https?://[^"]+)"', _html()):
        assert ref.startswith(_ALLOWED_LINK_PREFIXES), ref


def test_only_external_asset_is_the_google_fonts_stylesheet():
    # 脚本只能是本地的; 样式表只允许 Google Fonts 这一个(artifact-design 允许的唯一例外).
    assert re.findall(r'<script[^>]+src="https?://', _html()) == []
    links = re.findall(r'<link[^>]+href="(https?://[^"]+)"', _html())
    assert links == [_ALLOWED_STYLESHEET]
    assert "http" not in (SITE / "style.css").read_text(encoding="utf-8")


def test_afk_panel_matches_the_public_readme_facts():
    # 官网可以提 AFK(公开 README 本来就写了这个功能), 但细节要跟 README 对得上,
    # 不能悄悄夸大或漏掉限制条件.
    html = _html()
    afk = html[html.index('data-demo="6"'):]
    for fact in ("仅 Windows", "默认关闭", "350MB", "v1.1.1", "florr-auto-afk"):
        assert fact in afk, fact


def test_risk_notice_and_transparency_sections_are_present():
    html = _html()
    assert "账号被封" in html
    assert "它对游戏页面做了什么" in html
    assert "taskkill" in html            # 强制结束 Chrome 的说明不能丢
    assert "remote-debugging-port" in html


def test_download_section_replaces_run_from_source():
    html = _html()
    # "从源码运行" 不再是主路径(不再是导航项/按钮文案), 但仍允许作为下载失败时的备选
    # 提一句(FAQ / run-note 里的"...下载源码运行"), 所以只掐掉旧的导航链接和按钮文案.
    assert ">从源码运行<" not in html
    assert "目前还没有发布 exe" not in html
    assert 'id="download"' in html
    assert 'id="dl-btn"' in html and 'id="dl-btn-2"' in html
    assert 'id="dl-meta"' in html


def test_faq_section_has_the_expected_questions():
    html = _html()
    faq = html[html.index('id="faq"'):html.index('id="changelog"')]
    for q in ("杀毒软件报毒", "双击没反应", "能把文件夹直接发给朋友吗", "怎么更新",
              "运行时电脑还能干别的吗", "屏幕不是 1920×1080 能用吗", "有 Mac / Linux 版吗"):
        assert q in faq, q


def test_changelog_section_exists_and_js_can_fill_it():
    html = _html()
    assert 'id="changelog-list"' in html
    assert 'id="changelog-empty"' in html
    js = _js()
    assert "changelog-list" in js and "changelog-empty" in js
    for field in ("schema", "win64", "size", "sha256", "history", "published_at", "notes"):
        assert field in js, field


def test_feature_explorer_buttons_match_panels():
    html = _html()
    buttons = re.findall(r'class="feat-item[^"]*"\s+data-demo="(\d)"', html)
    panels = re.findall(r'class="stage-body[^"]*"\s+data-demo="(\d)"', html)
    assert len(buttons) == 7
    assert set(buttons) == set(panels)
    assert len(panels) == len(set(panels)), "面板 data-demo 不能重复"
    assert len(buttons) == len(set(buttons)), "按钮 data-demo 不能重复"

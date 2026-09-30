import json
from unittest.mock import patch, MagicMock

import pytest

import server_lookup
from server_lookup import fetch_server_ids, BIOME_INDEX, biome_key_for_map


def _mock_response(payload):
    """伪造urllib.request.urlopen()的返回值, 支持`with ... as resp:`用法
    (真代码里是`with urllib.request.urlopen(url) as resp:`)."""
    mock_resp = MagicMock()
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.read.return_value = json.dumps(payload).encode()
    return mock_resp


def test_fetch_server_ids_extracts_ids_from_all_three_regions():
    payload = {
        "servers": {
            "vultr-miami": {"id": "254j"},
            "vultr-frankfurt": {"id": "254k"},
            "vultr-tokyo": {"id": "254l"},
        }
    }
    with patch("server_lookup.urllib.request.urlopen", return_value=_mock_response(payload)):
        result = fetch_server_ids("desert")
    assert result == ["254j", "254k", "254l"]


def test_fetch_server_ids_uses_correct_biome_index_in_url():
    payload = {"servers": {"vultr-miami": {"id": "x"}, "vultr-frankfurt": {"id": "y"}, "vultr-tokyo": {"id": "z"}}}
    with patch("server_lookup.urllib.request.urlopen", return_value=_mock_response(payload)) as mock_urlopen:
        fetch_server_ids("desert")
    # 传给urlopen()的是一个Request对象(带自定义User-Agent, 见server_lookup.py
    # 里的注释——默认urllib UA会被接口的Cloudflare防护拦成403), 不是纯URL字符串.
    called_request = mock_urlopen.call_args[0][0]
    assert "florrio-map-1-green" in called_request.full_url  # desert就是BIOME_INDEX["desert"]==1
    assert called_request.get_header("User-agent")  # 确认真的带了UA, 不是裸请求


def test_fetch_server_ids_rejects_unknown_biome():
    with pytest.raises(KeyError):
        fetch_server_ids("not_a_real_biome")


@pytest.mark.parametrize("map_name, expected", [
    ("desert", "desert"),
    ("ocean", "ocean"),
    ("anthell", "ant_hell"),      # config 用 anthell, 接口 key 是 ant_hell
    ("garden", "garden"),
    ("jungle", "jungle"),
    ("sewers", "sewers"),         # 下水道有自己的服务器池(BIOME_INDEX 6)
    ("factory", "desert"),        # 工厂没有服务器池 —— 回退(生产代码不用这个函数)
    ("", "desert"),
])
def test_biome_key_for_map(map_name, expected):
    assert biome_key_for_map(map_name) == expected


def test_biome_index_covers_all_seven_biomes():
    # 跟油猴脚本源码里matrixs数组对齐: Garden/Desert/Ocean/Jungle/Ant Hell/Hel/Sewers,
    # 少一个/多一个/顺序错了都会导致某个生态区域查到别的区域的服务器.
    assert BIOME_INDEX == {
        "garden": 0, "desert": 1, "ocean": 2, "jungle": 3,
        "ant_hell": 4, "hel": 5, "sewers": 6,
    }


@pytest.mark.parametrize("index,biome", [
    (0, "garden"), (1, "desert"), (2, "ocean"), (4, "ant_hell"),
])
def test_biome_for_index_reverses_biome_index(index, biome):
    assert server_lookup.biome_for_index(index) == biome


def test_biome_for_index_unknown_is_none():
    assert server_lookup.biome_for_index(99) is None
    assert server_lookup.biome_for_index(None) is None


@pytest.mark.parametrize("biome,map_name", [
    ("desert", "desert"), ("ocean", "ocean"),
    ("ant_hell", "anthell"),     # 接口 key -> config/文件名
    ("garden", "garden"),        # 花园既是刷怪图也是蚁穴/下水道/工厂路线的第一段
    ("jungle", "jungle"), ("sewers", "sewers"),
])
def test_map_name_for_biome_reverses_biome_key_for_map(biome, map_name):
    assert server_lookup.map_name_for_biome(biome) == map_name


def test_map_name_for_biome_is_none_without_a_pathing_map():
    for biome in ("hel", "nope", None):
        assert server_lookup.map_name_for_biome(biome) is None

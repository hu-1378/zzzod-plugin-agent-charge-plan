"""芯片图标取色的用例。

留档截图只有一张电属性角色（安东），所以这里验证：电属性取色命中、
非属性条目不取色、色相区间自检、低饱和按物理处理。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from agent_charge_plan.operation import chip_element
from PIL import Image

#: 插件根（本文件在 tests/ 下）
PLUGIN_ROOT: Path = Path(__file__).resolve().parent.parent

#: 安东（电属性）的特训目标页截图
ANTON_SHOT = 'docs/screens/快捷手册/特训目标_安东_顶部.webp'
#: 「代理人技能」条目名中心（这张截图上实测的）
ANTON_SKILL_CENTER = (616, 817)


@pytest.fixture(scope='module')
def anton_screen() -> np.ndarray:
    """读成 BGR，和框架里的截图色序一致。"""
    img = Image.open(PLUGIN_ROOT / ANTON_SHOT).convert('RGB')
    return np.array(img)[:, :, ::-1]


def test_electric_icon_is_recognized(anton_screen: np.ndarray) -> None:
    """电属性角色的芯片图标应该判成电属性。"""
    element, detail = chip_element.read_chip_element(anton_screen, ANTON_SKILL_CENTER)

    assert element == '电属性', detail
    assert '色相' in detail


def test_icon_color_is_blue_for_electric(anton_screen: np.ndarray) -> None:
    """电属性的色相落在蓝色区间（实测 209°）。"""
    color = chip_element.read_icon_color(anton_screen, ANTON_SKILL_CENTER)

    assert color is not None
    hue, saturation, value = color
    assert 200 <= hue < 245
    assert saturation > 0.5


def test_only_skill_entry_is_read() -> None:
    """只有「代理人技能」这条才取色，经验书等材料条目不能取（会误判成属性）。"""
    assert chip_element.is_element_entry('代理人技能') is True
    assert chip_element.is_element_entry('代理人 技能') is True
    assert chip_element.is_element_entry('调查员记录') is False
    assert chip_element.is_element_entry('音擎能源') is False
    assert chip_element.is_element_entry('恶名·装甲哈提') is False


def test_for_entry_skips_non_skill(anton_screen: np.ndarray) -> None:
    """非属性条目直接返回 None，不去取色。"""
    element, detail = chip_element.read_chip_element_for_entry(
        anton_screen, '调查员记录', (616, 559)
    )
    assert element is None
    assert detail == ''


@pytest.mark.parametrize(
    ('hue', 'expected'),
    [
        (0, '火属性'),
        (20, '火属性'),
        (45, '流明属性'),
        (90, '风属性'),
        (150, '风属性'),
        (185, '冰属性'),
        (210, '电属性'),
        (260, '以太属性'),
        (300, '以太属性'),
        (350, '火属性'),
    ],
)
def test_hue_ranges(hue: float, expected: str) -> None:
    """七个属性的色相区间自检。"""
    assert chip_element.guess_element(hue, 0.9, 0.7) == expected


@pytest.mark.parametrize('saturation', [0.05, 0.15, 0.25])
def test_low_saturation_means_physical(saturation: float) -> None:
    """白灰图标没有明显色相，按物理处理。"""
    assert chip_element.guess_element(210, saturation, 0.8) == '物理属性'


def test_low_saturation_and_dark_is_unknown() -> None:
    """又灰又暗多半取到的是深色底，不硬判物理。"""
    assert chip_element.guess_element(210, 0.1, 0.2) is None


def test_read_icon_out_of_screen_returns_none() -> None:
    """条目在屏幕边缘、图标区被裁没了时安全返回 None。"""
    screen = np.zeros((100, 100, 3), dtype=np.uint8)
    assert chip_element.read_icon_color(screen, (5, 5)) is None

"""从「代理人技能」条目左侧的芯片图标判断属性。

## 为什么不用查表

项目的 `agent.py` 里属性数据有两个问题：
1. 新版本加的角色可能还没补进去；
2. 已经补进去的也可能标错（实测「比利」被标成电属性，游戏里是物理）。

图标颜色是游戏自己画的，比任何数据表都权威，而且不会滞后。

## 怎么取色

「代理人技能」这一条在清单里的排版是：

```
技能
  代理人技能              20-100        ← 条目名
  [芯片图标]  ▬▬▬▬ 153/203   前往        ← 图标在进度条左边
```

图标相对条目名中心大致偏左下（实测电属性那张：名字中心 (616,817)，图标中心约 (570,895)）。
取这个矩形里**饱和度高、亮度不太低**的像素，用它们的中位色相判断属性。

## 色相区间（初版，实测电属性 ≈ 210°）

按键值排，命中不了就返回 None 让调用方回退到查表。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from numpy import ndarray

#: 图标相对条目名中心的横向偏移（左边界、右边界）
ICON_OFFSET_X: tuple[int, int] = (-80, -8)
#: 图标相对条目名中心的纵向偏移（上边界、下边界）
ICON_OFFSET_Y: tuple[int, int] = (45, 110)

#: 只统计饱和度高于这个值的像素，避开深色底和灰色文字
MIN_SATURATION: float = 0.45
#: 只统计亮度高于这个值的像素，避开纯黑边框
MIN_VALUE: float = 0.35
#: 有效像素少于这个数就算取不到颜色（图标没渲染出来或被挡住）
MIN_PIXEL_COUNT: int = 60

#: (色相下界, 色相上界, 属性名)。区间左闭右开，按顺序匹配
HUE_RANGES: tuple[tuple[float, float, str], ...] = (
    (-1.0, 25.0, '火属性'),
    (25.0, 70.0, '流明属性'),
    (70.0, 165.0, '风属性'),
    (165.0, 200.0, '冰属性'),
    (200.0, 245.0, '电属性'),
    (245.0, 320.0, '以太属性'),
    (320.0, 361.0, '火属性'),
)

#: 白色 / 灰色的饱和度上限：物理属性的图标没有明显色相
PHYSICAL_MAX_SATURATION: float = 0.28


def read_icon_color(screen: ndarray, name_center: tuple[int, int]) -> tuple[float, float, float] | None:
    """取图标区域的主色，返回 (色相, 饱和度, 亮度)。

    色相单位是度（0~360），饱和度和亮度是 0~1。

    Args:
        screen: 整屏截图（BGR）
        name_center: 条目名的中心点（x, y）

    Returns:
        (h, s, v)；取不到足够饱和的像素时返回 None
    """
    height, width = screen.shape[:2]
    x1 = max(0, name_center[0] + ICON_OFFSET_X[0])
    x2 = min(width, name_center[0] + ICON_OFFSET_X[1])
    y1 = max(0, name_center[1] + ICON_OFFSET_Y[0])
    y2 = min(height, name_center[1] + ICON_OFFSET_Y[1])
    if x2 <= x1 or y2 <= y1:
        return None

    patch = screen[y1:y2, x1:x2]
    if patch.size == 0:
        return None

    # 截图是 BGR，转成 0~1 的 RGB 再算 HSV
    bgr = patch.astype(np.float32) / 255.0
    blue, green, red = bgr[:, :, 0], bgr[:, :, 1], bgr[:, :, 2]
    max_c = np.maximum(np.maximum(red, green), blue)
    min_c = np.minimum(np.minimum(red, green), blue)
    delta = max_c - min_c
    value = max_c
    saturation = np.where(max_c > 0, delta / np.maximum(max_c, 1e-6), 0.0)

    hue = np.zeros_like(max_c)
    with np.errstate(divide='ignore', invalid='ignore'):
        hue = np.where(
            delta == 0, 0.0,
            np.where(
                max_c == red, ((green - blue) / np.maximum(delta, 1e-6)) % 6,
                np.where(
                    max_c == green, (blue - red) / np.maximum(delta, 1e-6) + 2,
                    (red - green) / np.maximum(delta, 1e-6) + 4,
                ),
            ),
        )
    hue = (hue * 60.0) % 360.0

    mask = (saturation >= MIN_SATURATION) & (value >= MIN_VALUE)
    if int(mask.sum()) < MIN_PIXEL_COUNT:
        return None

    return (
        float(np.median(hue[mask])),
        float(np.median(saturation[mask])),
        float(np.median(value[mask])),
    )


def guess_element(hue: float, saturation: float, value: float) -> str | None:
    """按色相判断属性。

    Args:
        hue: 色相（度）
        saturation: 饱和度（0~1）
        value: 亮度（0~1）

    Returns:
        属性名（如「电属性」）；判断不了返回 None
    """
    if saturation < PHYSICAL_MAX_SATURATION:
        # 白灰图标 = 物理。亮度太低说明取到的其实是深色底，不当物理处理
        return '物理属性' if value > 0.5 else None

    for low, high, element in HUE_RANGES:
        if low <= hue < high:
            return element
    return None


def read_chip_element(screen: ndarray, name_center: tuple[int, int]) -> tuple[str | None, str]:
    """取图标颜色并判断属性。

    Args:
        screen: 整屏截图（BGR）
        name_center: 条目名的中心点

    Returns:
        (属性名或 None, 给人看的说明)。说明里带上取到的 h/s/v，方便实机核对
    """
    color = read_icon_color(screen, name_center)
    if color is None:
        return None, '芯片图标区没取到足够饱和的颜色'

    hue, saturation, value = color
    element = guess_element(hue, saturation, value)
    detail = f'图标色相 {hue:.0f}° 饱和 {saturation:.2f} 亮度 {value:.2f} -> {element or "认不出"}'
    return element, detail


#: 只有这一种条目按芯片属性分副本；别的条目（经验书、认证章、驱动盘）左侧图标是材料图标，
#: 颜色代表稀有度不是属性，取色会误判（实测把经验书判成了火属性）
ELEMENT_ENTRY_NAME: str = '代理人技能'


def is_element_entry(entry_name: str) -> bool:
    """这条目是不是「按属性分副本」的那种。

    Args:
        entry_name: 条目名（画面上的文字）

    Returns:
        True 表示可以按图标颜色判属性
    """
    return entry_name.replace(' ', '') == ELEMENT_ENTRY_NAME


def read_chip_element_for_entry(
    screen: ndarray, entry_name: str, name_center: tuple[int, int]
) -> tuple[str | None, str]:
    """只在「代理人技能」这条上读图标属性，其余条目直接返回 None。

    Args:
        screen: 整屏截图（BGR）
        entry_name: 条目名
        name_center: 条目名中心点

    Returns:
        (属性名或 None, 说明)
    """
    if not is_element_entry(entry_name):
        return None, ''
    return read_chip_element(screen, name_center)

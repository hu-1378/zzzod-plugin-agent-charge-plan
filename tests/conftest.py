"""插件内 pytest 用例的公共夹具。

几条约定：

- 截图直接用仓库里的 `docs/screens/**.webp`（1080p，UID 已打码），不另存副本。
- OCR 用真模型（`OnnxOcrMatcher()`，默认 CPU），所以第一次加载约 1 秒，走 session 级夹具复用一个实例。
- 不初始化完整的 `ZContext`：那会拉配置、探测游戏窗口，慢而且要跑起来的游戏环境。
  这里造一个只有 `ocr` / `screen_loader` / `compendium_service` 三个属性的最小替身，
  正好覆盖被测代码实际用到的那几个入口。
- 不碰真实用户配置：`ChargePlanConfig` 相关的用例统一把工作目录指到 `tmp_path`。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest
import yaml
from PIL import Image

if TYPE_CHECKING:
    from one_dragon.base.geometry.rectangle import Rect
    from one_dragon.base.matcher.ocr.onnx_ocr_matcher import OnnxOcrMatcher
    from zzz_od.game_data.compendium import CompendiumService


#: 仓库根目录。本文件在 plugins/agent_charge_plan/tests/ 下，往上四层就是仓库根
REPO_ROOT: Path = Path(__file__).resolve().parents[3]
#: 插件根目录
PLUGIN_ROOT: Path = REPO_ROOT / 'plugins' / 'agent_charge_plan'
#: 留档截图根目录
SCREEN_DIR: Path = PLUGIN_ROOT / 'docs' / 'screens'
#: 特训目标页的截图（顶部 / 滚动中 / 滚到底 / 只剩驱动盘 / 驱动盘分组）
PLAN_SCREEN_DIR: Path = SCREEN_DIR / '快捷手册'
#: 更换特训目标弹窗的截图
DIALOG_SCREEN_DIR: Path = SCREEN_DIR / '特训目标-更换特训目标'

# 主仓是 src-layout 且 uv 里 package=false，插件代码要能被 import 得手动加路径。
# `plugins` 加进 sys.path 后，插件按 `agent_charge_plan.operation.xxx` 的虚线名导入，
# 和一条龙加载第三方插件时的模块命名一致。
for _path in (REPO_ROOT / 'src', REPO_ROOT / 'plugins'):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


class NullScreenLoader:
    """假装主仓 screen_info 里没有这些区域，逼被测代码走插件自带的兜底坐标。"""

    def get_area(self, screen_name: str, area_name: str) -> None:
        """统一返回 None：主仓没定义该区域。

        Args:
            screen_name: 画面名，这里不看
            area_name: 区域名，这里不看

        Returns:
            永远 None
        """
        return None


class StubContext:
    """最小的「一条龙上下文」替身。

    只放被测代码真正会读的三个属性，避免 `ZContext()` 那一堆初始化。
    """

    def __init__(
        self,
        ocr: OnnxOcrMatcher,
        compendium_service: CompendiumService | None = None,
        screen_loader: NullScreenLoader | PluginScreenLoader | None = None,
    ) -> None:
        """初始化。

        Args:
            ocr: 真 OCR 匹配器
            compendium_service: 副本表服务；不需要副本表时传 None
            screen_loader: 区域查找表；不传就用「主仓没有这些区域」的替身
        """
        self.ocr: OnnxOcrMatcher = ocr
        self.screen_loader: NullScreenLoader | PluginScreenLoader = (
            screen_loader if screen_loader is not None else NullScreenLoader()
        )
        self.compendium_service: CompendiumService | None = compendium_service


class _AreaStub:
    """`screen_loader.get_area()` 的返回值替身：只需要 `.rect`。"""

    def __init__(self, rect: Rect) -> None:
        """初始化。

        Args:
            rect: 区域矩形
        """
        self.rect: Rect = rect


class PluginScreenLoader:
    """用插件自带 screen_info yml 当区域查找表。

    真 `ScreenLoader` 要先起配置、注册画面；这里只按同样的 yml 结构建一张
    `(画面名, 区域名) → 矩形` 的表。好处是测试和实机取的是同一份坐标：
    yml 里坐标改了，用例会立刻跟着变。
    """

    def __init__(self, screen_info_dir: Path) -> None:
        """读入 `screen_info_dir` 下所有 yml。

        Args:
            screen_info_dir: 插件自带 screen_info 目录
        """
        from one_dragon.base.geometry.rectangle import Rect

        self.area_map: dict[tuple[str, str], Rect] = {}
        for yml_path in sorted(screen_info_dir.glob('*.yml')):
            data: Any = yaml.safe_load(yml_path.read_text(encoding='utf-8'))
            screen_name = data.get('screen_name', '')
            for area in data.get('area_list', []):
                left, top, right, bottom = area['pc_rect']
                self.area_map[(screen_name, area['area_name'])] = Rect(left, top, right, bottom)

    def get_area(self, screen_name: str, area_name: str) -> _AreaStub | None:
        """查一个区域。查不到返回 None（和真 ScreenLoader 的行为一致）。

        Args:
            screen_name: 画面名
            area_name: 区域名

        Returns:
            带 `.rect` 的替身；没配置就是 None
        """
        rect = self.area_map.get((screen_name, area_name))
        if rect is None:
            return None
        return _AreaStub(rect)


def load_screen(path: Path) -> np.ndarray:
    """读一张 webp 截图，转成项目里到处用的 BGR 数组。

    Args:
        path: 截图路径

    Returns:
        BGR 图像数组
    """
    return np.array(Image.open(path).convert('RGB'))[:, :, ::-1]


def read_screen_area(yml_name: str, area_name: str) -> Rect:
    """直接从插件自带的 screen_info yml 里取一个区域的矩形。

    不走 `ScreenLoader`：那要先起一套配置、注册画面，太重。这里只按同样的 yml 结构取坐标，
    顺带保证「文档里写的坐标」和「测试用的坐标」是同一份数据。

    Args:
        yml_name: screen_info 下的 yml 文件名
        area_name: 区域名

    Returns:
        区域矩形
    """
    from one_dragon.base.geometry.rectangle import Rect

    data: dict[str, Any] = yaml.safe_load(
        (PLUGIN_ROOT / 'screen_info' / yml_name).read_text(encoding='utf-8')
    )
    for area in data['area_list']:
        if area['area_name'] == area_name:
            left, top, right, bottom = area['pc_rect']
            return Rect(left, top, right, bottom)
    raise KeyError(f'{yml_name} 里没有区域 {area_name}')


@pytest.fixture(scope='session')
def ocr() -> OnnxOcrMatcher:
    """整个测试会话共用一个真 OCR 实例（CPU 推理，不需要显卡）。"""
    from one_dragon.base.matcher.ocr.onnx_ocr_matcher import OnnxOcrMatcher

    matcher = OnnxOcrMatcher()
    assert matcher.init_model(), (
        'OCR 模型加载失败，先确认 assets/models/onnx_ocr/ppocrv6 下 det.onnx / rec.onnx 在'
    )
    return matcher


@pytest.fixture(scope='session')
def compendium_service() -> CompendiumService:
    """真副本表服务。只读 assets/game_data/compendium_data.yml，很轻。"""
    from zzz_od.game_data.compendium import CompendiumService

    service = CompendiumService()
    service.reload()
    assert service.get_category_list_data('训练'), '副本表没读到「训练」TAB，检查 assets/game_data'
    return service


@pytest.fixture(scope='session')
def bare_ctx(ocr: OnnxOcrMatcher) -> StubContext:
    """只有 OCR 的上下文，给画面解析用例用（不需要副本表）。"""
    return StubContext(ocr)


@pytest.fixture(scope='session')
def full_ctx(ocr: OnnxOcrMatcher, compendium_service: CompendiumService) -> StubContext:
    """带副本表的上下文，给映射层用例用。"""
    return StubContext(ocr, compendium_service)


@pytest.fixture(scope='session')
def plan_screen_path_list() -> list[Path]:
    """特训目标页的全部留档截图，按文件名排序。"""
    paths = sorted(PLAN_SCREEN_DIR.glob('*.webp'))
    assert len(paths) >= 5, f'{PLAN_SCREEN_DIR} 下应该有 5 张特训目标页截图，实际 {len(paths)} 张'
    return paths


@pytest.fixture(scope='session')
def plan_screen(plan_screen_path_list: list[Path]) -> dict[str, np.ndarray]:
    """特训目标页截图，键是文件名（不含后缀）。一次会话只读一次盘。"""
    return {path.stem: load_screen(path) for path in plan_screen_path_list}


class PlanPageData:
    """一张特训目标页截图读下来的全部结果，省得每个用例各跑一遍 OCR。"""

    def __init__(
        self,
        name: str,
        screen: np.ndarray,
        blocks: list[Any],
        items: list[Any],
        agent_name: str,
    ) -> None:
        """初始化。

        Args:
            name: 截图文件名（不含后缀）
            screen: BGR 截图
            blocks: 清单区 OCR 出来的文字块
            items: 解析出来的条目
            agent_name: 顶部角色卡上的代理人名字
        """
        self.name: str = name
        self.screen: np.ndarray = screen
        self.blocks: list[Any] = blocks
        self.items: list[Any] = items
        self.agent_name: str = agent_name


@pytest.fixture(scope='session')
def plan_pages(
    bare_ctx: StubContext,
    plan_screen: dict[str, np.ndarray],
    plan_list_rect: Rect,
) -> dict[str, PlanPageData]:
    """5 张特训目标页截图各读一遍，结果按文件名存起来。每次测试会话只跑一次 OCR。"""
    from agent_charge_plan.operation import agent_plan_page

    pages: dict[str, PlanPageData] = {}
    for name, screen in plan_screen.items():
        blocks = agent_plan_page.read_text_blocks(bare_ctx, screen, plan_list_rect)
        items = agent_plan_page.parse_plan_items(blocks, plan_list_rect)
        agent_name = agent_plan_page.read_agent_name(bare_ctx, screen)
        pages[name] = PlanPageData(name, screen, blocks, items, agent_name)
    return pages


@pytest.fixture(scope='session')
def dialog_screen() -> np.ndarray:
    """更换特训目标弹窗截图。"""
    paths = sorted(DIALOG_SCREEN_DIR.glob('*.webp'))
    assert len(paths) == 1, f'{DIALOG_SCREEN_DIR} 下应该正好 1 张弹窗截图，实际 {len(paths)} 张'
    return load_screen(paths[0])


@pytest.fixture(scope='session')
def material_calc_screen() -> np.ndarray:
    """养成材料计算弹窗的截图，只拿来做「这不是特训目标页」的反例。"""
    paths = sorted((SCREEN_DIR / '养成材料计算').glob('*.webp'))
    assert paths, f'{SCREEN_DIR / "养成材料计算"} 下没有截图'
    return load_screen(paths[0])


@pytest.fixture(scope='session')
def plugin_screen_loader() -> PluginScreenLoader:
    """插件自带 screen_info 的区域查找表（改 yml 坐标会直接影响用例）。"""
    return PluginScreenLoader(PLUGIN_ROOT / 'screen_info')


@pytest.fixture(scope='session')
def plugin_ctx(
    ocr: OnnxOcrMatcher,
    compendium_service: CompendiumService,
    plugin_screen_loader: PluginScreenLoader,
) -> StubContext:
    """带插件 screen_info 区域的上下文，给材料缺口这类需要取区域的用例用。"""
    return StubContext(ocr, compendium_service, plugin_screen_loader)


@pytest.fixture(scope='session')
def guide_screen() -> np.ndarray:
    """养成方案详情页截图（右侧「材料预览」栏）。"""
    paths = sorted((SCREEN_DIR / '养成方案-详情').glob('*.webp'))
    assert paths, f'{SCREEN_DIR / "养成方案-详情"} 下没有截图'
    return load_screen(paths[0])


@pytest.fixture(scope='session')
def agent_guide_screen() -> np.ndarray:
    """养成指南页截图（方案概览）：材料计算弹窗那条路径的入口页。"""
    paths = sorted((SCREEN_DIR / '养成指南').glob('*.webp'))
    assert paths, f'{SCREEN_DIR / "养成指南"} 下没有截图'
    return load_screen(paths[0])


@pytest.fixture(scope='session')
def calc_result_screen() -> np.ndarray:
    """养成材料计算结果页：四列明细表。"""
    path = SCREEN_DIR / '养成材料计算' / '结果_明细四列表.webp'
    assert path.exists(), f'{path} 不存在'
    return load_screen(path)


@pytest.fixture(scope='session')
def calc_need_icon_screen() -> np.ndarray:
    """养成材料计算结果页：页面停在顶部，图标横排和表格同时在框里。"""
    path = SCREEN_DIR / '养成材料计算' / '结果_需要获取的材料.webp'
    assert path.exists(), f'{path} 不存在'
    return load_screen(path)


@pytest.fixture(scope='session')
def calc_setting_screen() -> np.ndarray:
    """养成材料计算设置页（结果页还没出来），用来验证「读不到就记 warning」。"""
    path = SCREEN_DIR / '养成材料计算' / '设置_上半.webp'
    assert path.exists(), f'{path} 不存在'
    return load_screen(path)


@pytest.fixture(scope='session')
def plan_list_rect(bare_ctx: StubContext) -> Rect:
    """清单区矩形（主仓没定义时用插件兜底常量）。"""
    from agent_charge_plan.operation import screen_areas

    return screen_areas.get_area_rect(
        bare_ctx, screen_areas.COMPENDIUM_SCREEN, screen_areas.AREA_PLAN_LIST
    )


@pytest.fixture(scope='session')
def left_cat_rect(bare_ctx: StubContext) -> Rect:
    """左侧分类「特训目标」那一格的矩形。"""
    from agent_charge_plan.operation import screen_areas

    return screen_areas.get_area_rect(
        bare_ctx, screen_areas.COMPENDIUM_SCREEN, screen_areas.AREA_LEFT_CAT_TARGET
    )


@pytest.fixture(scope='session')
def dialog_grid_rect() -> Rect:
    """更换弹窗里代理人网格的矩形，坐标取自插件自带 screen_info。"""
    return read_screen_area('agent_plan_change_target.yml', '区域-代理人网格')

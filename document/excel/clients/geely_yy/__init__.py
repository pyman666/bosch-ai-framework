"""吉利 (geely) yy 多车型缺口报表解析: 复用通用 wide-excel 引擎.

业务上 yy 是一个**多车型**的零件需求报表:
- raw R1: 车型 group label (e.g. 'CX11 A3 L', 'DX11 L', 'DS11 A1' ...).
- raw R2: 总计行 (汇总数字, 业务方不消费).
- raw R3: 真正的列名行 (id 列名 + '需求合计' 总计列名 + 各车型对应日期).
- raw R4+: 每行一个零件 (供应商代码 / 仓库 / 当前库存 + 各 (车型, 日期) 的需求数).

模板**完全固定**, 只需要喂一份 ``WideExcelConfig``, 通用层 (Python-first +
LLM-fallback) 自动:

- ``ExcelConfig.row_filter`` 不跳行 (R1 车型 group 要保留, R2 总计行被
  ``data_start_1b > 2`` + ``primary_id`` 主 id 空判一刀就扔了).
- ``ExcelConfig.col_filter`` 不显式过滤 (绝大多数 id 列在源文件里被隐藏, ``read_hidden=False``
  自动剔除, 剩下的 ``物料编码`` / ``供应商代码1`` / ``仓库`` / ``库存合计`` / ``在途``
  + 全部动态列正好是业务方关心的).
- ``header_config(row=3, rows=3)``: clean R3 是表头, 数据从 clean R4 开始.
- ``var_pattern=r"^20\\d{2}-\\d{2}-\\d{2}"``: 在 R3 的 cell 上做 regex 测试, 命中 ISO 日期
  即视作动态列 (R3 col 6+); '需求合计' (R3 col 6) / '1月份需求'/'2月份需求'/'3月份需求'
  (R3 col 150-168) 不匹配, 自动丢弃.
- ``var_header_rows=[1, 3]``: 动态列的 ``var_value`` = ``"<R1 车型>/<R3 日期>"`` 拼合,
  e.g. ``"CX11 A3 L/2025-12-30"``. 业务方拿到 ``data: list[{var, qty}]`` 后按 ``"/"``
  split 即可分别取车型和日期.
- ``read_hidden=False``: 779/792 行隐藏, 必须跳 (供应商表里把所有零件备好后只 unhide
  本批次).
"""
from ...core import ExcelConfig, HeaderConfig
from ...wide import WideExcelConfig
from .. import support
from .schemas import GeelyYyData, GeelyYyRow


# 业务方约定的 id 列 (Excel 中文列名 -> JSON 字段名).
# **顺序约定**: 第一个 key 是 ``primary_id`` 默认值. yy 的主键是 "物料编码"
# (R2 总计行该列为空, 用它过滤一刀干净).
GEELY_YY_ID_MAP = {
    "物料编码": "partNo",
    "供应商代码1": "supplierCode",
    "仓库": "warehouse",
    "车型": "carModel",
}


GEELY_YY_CONFIG = WideExcelConfig(
    label="geely-yy",
    description=(
        "按吉利 yy 月度生产计划表的固定模板 (零件号 + 项目分组 + 多车型 × 多日期的二维需求量)"
    ),
    id_map=GEELY_YY_ID_MAP,
    row_schema=GeelyYyRow,
    excel_config=ExcelConfig(
        header_config=HeaderConfig(
            rows=3,
            row=3,
            block={"车型": 1, "date": 3},
        ),
    ),
    var_pattern=r"^20\d{2}-\d{2}-\d{2}",
    var_name="date",
    value_name="qty",
)


support(GEELY_YY_CONFIG)



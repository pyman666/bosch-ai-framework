"""吉利 (geely) cd 总装上线计划汇总解析: 复用通用 wide-excel 引擎.

业务上 cd = 总装上线计划汇总 (sheet "总装上线计划汇总"), 同一 sheet 上下两段:

- **顶部车型汇总段** (raw R6-R21, **本 client 关心**): 每行一个 (车型, 内饰颜色) 组合,
  后面 31 个 ISO 日期列 = 当月每天的整车上线计划数. col1 (品牌) / col2 (车型) 在该
  段是 ``A6:A21`` / ``B6:B21`` 大合并 cell, ``matrix_filled`` 后整段 col2 全部被
  填成 ``'车型'`` (top-left cell value).
- **下方详情段** (raw R22+, **不抽**): 按 (品牌=领克国内/车型=BX11 A3/配置=耀 TOP/
  内饰颜色=黑蓝内饰) 全维度拆分的逐细分行, col2 是 ``'BX11 A3'`` / ``'NL-4'`` /
  ``'星愿 E22H'`` 等真实车型名, **不含 ``'车型'`` 子串**, 用这条规则正好把汇总段切出来.

模板**完全固定**, 只需要喂一份 ``WideExcelConfig``, 通用层 (Python-first +
LLM-fallback) 自动:

- ``ExcelConfig.row_filter`` 用 col2 (raw 第 2 列) 含 ``'车型'`` 子串保留: 过滤后
  矩阵只剩 R6-R21 (16 行), R18-R20 隐藏 ``read_hidden=False`` 自动剔除, 实剩 13 行.
- ``ExcelConfig.col_filter`` 不显式过滤 (整个 sheet 36 列里 1-4 是 id 列, 5 是月度
  合计, 6-36 是日期列, 全部保留, var_pattern 自然挑出动态列).
- ``header_config(row=1, rows=3)``: clean R1 (= raw R6) 是表头, 跳前 3 行 = 跳掉
  R7 (周一二三 markers, raw weekday number) + R8 (合计行), 数据从 clean R4
  (= raw R9) 起. R8 的 col2 经 ``matrix_filled`` 后也是 ``'车型'`` (在合并段内),
  靠 row_filter 进不来不行 -- header_config 跳行兜住.
- ``var_pattern=r"^20\\d{2}-\\d{2}-\\d{2}"``: 在 clean R1 (raw R6) 的 cell 上做
  regex 测试, 命中 ISO 日期即视作动态列. col 5 ('1月' 月度合计) 不匹配自然丢; 这样
  下个月文件变成 '2月' 也不会挂 (id_map 不写死月份名, 月度合计业务方拿到 ``data``
  自己 ``sum`` 即可).
- ``read_hidden=False``: R2-5 隐藏 metadata + R18-20 (E335 国内/亚太) 隐藏明细自动跳.
"""
from ...core import ExcelConfig, FilterBlock, FilterConfig, HeaderConfig
from ...wide import WideExcelConfig
from .. import support
from .schemas import GeelyCdData, GeelyCdRow  # noqa: F401


# 业务方约定的 id 列 (Excel 中文列名 -> JSON 字段名). Python 路径会在 clean matrix
# 表头行精确匹配这些 keys, 任何一个找不到就走 LLM 兜底重新定位.
GEELY_CD_ID_MAP = {
    "配置": "carModel",
    "内饰颜色": "color",
}


GEELY_CD_CONFIG = WideExcelConfig(
    label="geely-cd",
    description=(
        "按吉利 cd 总装上线计划汇总表的固定模板, 只抽顶部 '车型汇总' 段 (各车型 / "
        "内饰颜色 + 当月每天的上线计划数). 下方按 '配置' 拆分的逐细分段 + 合计行 "
        "+ 周markers 行不要"
    ),
    id_map=GEELY_CD_ID_MAP,
    row_schema=GeelyCdRow,
    excel_config=ExcelConfig(
        row_filter=FilterConfig(
            axis="row",
            mode="use",
            blocks=[FilterBlock(refer_index=2, refer_name="车型")],
        ),
        header_config=HeaderConfig(rows=3),
    ),
    var_pattern=r"^20\d{2}-\d{2}-\d{2}",
    var_name="date",
    value_name="qty",
)


support(GEELY_CD_CONFIG)



"""吉利 (geely) ms 跟踪表解析: 复用通用 wide-excel 引擎.

业务上 ms = 物料需求跟踪表 (sheet 名 "跟踪表"), 模板由供应商定, 横向是
``(id 列 + 当前库存指标 + 累计/累积消耗 + 试制阶段 + 需求合计 + 结余)``,
其中只有 "需求合计" 那段 (按日期一列一格) 是业务方关心的动态数据. 模板**完全
固定**, 只需要喂一份 ``WideExcelConfig``, 通用层 (Python-first + LLM-fallback)
自动:

- ``ExcelConfig.row_filter`` 跳掉 R1 (group label 行).
- ``ExcelConfig.col_filter`` 用 R1 上的 group 标签 "需求合计" 限制动态列范围
  (右侧 "结余" group 是干扰); 同时 ``positions=[1..15]`` 涵盖所有 id 列实际列号
  (隐藏列 raw col 8-13, 18-26 会自动剔除).
- ``ExcelConfig.header_config`` 指定 clean R1 (= raw R2) 是表头, 数据从 clean R2
  开始 (clean R2 = raw R3 全空 / clean R3 = raw R4 全 #REF!, 都会被通用层
  "id 全 null 跳过 / qty 全非数字跳过" 自动忽略).
- ``var_pattern`` 匹配 ISO 日期格式的列头.
- ``read_hidden=False`` 跳掉 1038+ 隐藏行 (供应商习惯把全量数据塞进去隐藏 +
  unhide 只保留本批次).
"""
from ...core import ExcelConfig, FilterBlock, FilterConfig, HeaderConfig
from ...wide import WideExcelConfig
from .. import support
from .schemas import GeelyMsData, GeelyMsRow


# 业务方约定的 id 列 (Excel 中文列名 -> JSON 字段名). Python 路径会在 clean matrix
# 表头行精确匹配这些 keys, 任何一个找不到就走 LLM 兜底重新定位.
GEELY_MS_ID_MAP = {
    "物料号": "partNo",
    "物料描述": "partDesc",
    "使用车间": "workshop",
    "供应商编码": "supplierCode",
}


GEELY_MS_CONFIG = WideExcelConfig(
    label="geely-ms",
    description=(
        "按吉利 ms 物料需求跟踪表的固定模板 (物料号 + 库存阈值 + 供应商分工 + 未来的需求合计)"
    ),
    id_map=GEELY_MS_ID_MAP,
    row_schema=GeelyMsRow,
    excel_config=ExcelConfig(
        col_filter=FilterConfig(
            axis="column",
            mode="use",
            before=15,
            blocks=[FilterBlock(refer_index=1, refer_name="需求合计")],
        ),
        header_config=HeaderConfig(row=2, rows=4),
    ),
    var_pattern=r"^20\d{2}-\d{2}-\d{2}",
    var_name="date",
    value_name="qty",
)


support(GEELY_MS_CONFIG)



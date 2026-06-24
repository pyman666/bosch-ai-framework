"""吉利 (geely) hzw3 报表解析: 复用通用 wide-excel 引擎.

业务上 hzw3 = "缺口信息" 报表, 模板由供应商定, 横向是 (id 列 + 当前库存指标 + 未来
若干天的缺口数), 纵向是不同零件. 模板**完全固定**, 只需要喂一份 ``WideExcelConfig``,
通用层 (Python-first + LLM-fallback) 自动:

- ``ExcelConfig.row_filter`` 跳掉 R1-R4 (banner + 占位行).
- ``ExcelConfig.col_filter`` 用 R2 上的 group 标签 "缺口信息" 限制动态列范围 (那边
  右侧还有 "生产计划" group 是干扰, 不需要).
- ``ExcelConfig.header_config`` 指定真正的表头是 clean R1 (= raw R5), 但因为 R5:R6
  合并 (B5:B6 合并 -> "零件号" 跨两行), 数据从 clean R3 (= raw R7) 才开始, 所以
  ``rows=2`` 让通用 ``_extract_rows_wide`` 跳过表头第二行.
- ``var_pattern`` 匹配 ISO 日期格式的列头 (e.g. ``"2025-12-28 00:00:00"``).
- ``read_hidden=False`` 跳掉 470+ 隐藏行 (供应商常会复制旧月份隐藏起来).

供应商哪天悄悄改了 "零件号" 叫 "物料号" / 把 "缺口信息" 改成 "缺口预警" / 表头多
塞了一行 banner... Python 路径会失败, 通用层一次 LLM 调用拿到 ``WideExcelRepairPlan``
重跑, 业务方收到一段 "我看了下您上传的文件, 之前约定的 ..." 友好 prose, 流程不卡.
"""
from ...core import ExcelConfig, FilterBlock, FilterConfig, HeaderConfig
from ...wide import WideExcelConfig
from .. import support
from .schemas import GeelyHzw3Data, GeelyHzw3Row  # noqa: F401


# 业务方约定的 id 列 (Excel 中文列名 -> JSON 字段名). Python 路径会在 clean matrix
# 表头行精确匹配这些 keys, 任何一个找不到就走 LLM 兜底.
GEELY_HZW3_ID_MAP = {
    "零件号": "partNo",
}


GEELY_HZW3_CONFIG = WideExcelConfig(
    label="geely-hzw3",
    description=(
        "按吉利 hzw3 缺口信息表的固定模板 (零件号 + 当前库存指标 + 未来几天的缺口数, 负数表示缺料)"
    ),
    id_map=GEELY_HZW3_ID_MAP,
    row_schema=GeelyHzw3Row,
    excel_config=ExcelConfig(
        col_filter=FilterConfig(
            axis="column",
            mode="skip",
            before=1,
            blocks=[FilterBlock(refer_index=2, refer_name="缺口信息")],
        ),
        header_config=HeaderConfig(rows=6, row=5),
    ),
    var_pattern=r"^20\d{2}-\d{2}-\d{2}",
    var_name="date",
    value_name="qty",
)


support(GEELY_HZW3_CONFIG)



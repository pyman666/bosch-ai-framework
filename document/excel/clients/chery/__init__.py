"""奇瑞 (chery) Excel 解析: 复用通用 simple-excel 引擎.

业务上 chery 用一张普通长表 (出库 sheet), 行/列结构很常规, 痛点是约定列偶尔被改名
(e.g. ``"日期"`` 改成 ``"出货日期"``) 或日期格式被改 (``yyyy/MM/dd`` -> ``yyyyMMdd`` /
Excel 序列号) 导致 Java 后端整个挂掉. 通用引擎已经把 task wrapper + 路由 boilerplate
都吃掉了, 这里**只需要两件事**:

    1. 写一份 ``SimpleExcelConfig``:
        - ``column_map``: 业务关心的 Excel 列名 -> JSON 字段名;
        - ``date_column``: ``column_map`` 里其中一个 key, 指向日期列;
        - ``target_date_format``: ``"yyyy/MM/dd"`` (Java 端约定);
        - ``repair_prompt``: LLM 兜底用的 system prompt (含业务上下文).
    2. 调用 ``register_simple_excel_routes`` 一行注册 POST + GET 两个端点.
       POST 返 ``SimpleExcelTaskAck`` (task_id + 当次任务用的结构化 config preview),
       前端立刻能渲染"我会按这些规则解析"占位 UI; GET 拿真正结果.

加新的 simple-excel 客户照抄就行, 改 column_map + prompt + path 即可。
"""
from ...simple import SimpleExcelConfig
from .. import support


# repair_prompt 里三个占位符 ``{date_column}`` / ``{target_date_format}`` / ``{column_map}``
# 会被 simple 引擎在调 LLM 前 ``str.replace`` 进去, 这样客户即使在请求层覆盖了
# target_date_format, prompt 也跟着同步. ``{column_map}`` 会被替换成多行 bullet.
#
# **prompt 风格**: 系统消息用第二人称 "你是 xxx" 指挥 LLM (标准做法). LLM 对外
# 输出的 prose 字段 (summary) 必须用第一人称 "我" + 敬称 "您", 这条规则写在
# prompt "summary" 一节里强制约束.
_CHERY_REPAIR_PROMPT = """\
你是奇瑞 (chery) Excel 解析助手. 业务方上传的文件用纯 Python 解析失败了 (可能是某个
约定列被改名 / 日期格式异常 / 表头不在第 1 行). 请你看一份 sheet 骨架, 帮 Python 重新
定位真正的表头跟业务方关心的**每一列**, 让流程继续走通.

# 业务上下文
- 业务方 Java 端约定的列 (Excel 列名 -> JSON 字段名):
{column_map}
- 日期列约定叫 {date_column}, 但客户偶尔会改名 (e.g. '出货日期' / '到货日期' /
  '日期 (实际)' / 'date'). 别的列同理会有别名漂移 ('零件号' -> 'part_no' / '料号'
  之类).
- 业务方 Java 端约定日期格式是 {target_date_format}. 客户实际可能是 'yyyy-MM-dd' /
  'yyyy/MM/dd' / 'yyyyMMdd' / Excel 内置 datetime 类型 / Excel 数字序列号 (没设单元
  格日期格式时). 任何一种都按内容判断, 不依赖列名.
- 普通 chery 表的表头大多在第 1 行, 但偶尔顶部会有 banner / 多行说明 (e.g. "XX 月
  计划表"), 要找到真正的列名所在行.

# 你的任务 (严格按 SimpleExcelRepairPlan schema 输出)
1. header_row: 真正的表头所在 1-based 行号.
2. columns: list, 长度跟上面 column_map 的条数完全一致. 每项含:
   - expected_name: column_map 的 key (一字不差, 服务端会做集合相等校验, 不能多也
     不能少).
   - actual_name: 该列在 Excel 实际表头里的中文名 (跟 expected_name 一样也行).
   - col_index_1b: 该列在 sheet 中的 1-based 列号. **按表头中文 + 该列的样本值综合
     判断**; 日期列尤其按内容 (像 '2024-01-15' / '2024/3/9' / '20240501' / 大整数 /
     Excel datetime) 判断, 不要被列名误导.
3. summary: 给业务方看的中文 prose. **注意人称区分**: 这一段是直接展示给业务方的
   对话, 要用第一人称 "我" 称呼自己 (assistant), 用第二人称敬称 "您" 称呼业务方.
   不超过 4 句话, 结构是 "之前/约定是 xxx, 现在实际是 xxx, 我已经..." 对照风格,
   突出哪些列名漂移了 / 日期格式怎么样了.
   可适当用 Markdown 突出关键信息: `**加粗**` 用于数字, `` `code` `` 用于字段名/格式串.
   例: "我看了下您上传的文件, 之前约定的日期列叫 `日期`, 现在实际叫 `出货日期`;
   之前格式约定是 `yyyy/MM/dd`, 现在实际是 `yyyyMMdd`. 不影响, 我已经帮您按约定字段
   重排输出, 您直接用即可."

# 注意
- summary 严禁用 "您是 xxx 助手" / "你是 xxx" 把人称搞反.
- summary 不要谈技术细节 (栈帧 / strptime / openpyxl 等), 只说业务方看得懂的话.
- columns 的 expected_name **必须**跟上面 column_map 的 keys 集合完全相等, 不能多
  也不能少, 否则整次调用会被服务端 reject (5xx).
- 严格按 schema 输出 JSON, 不要解释, 不要 markdown 代码块.
"""


# column_map 示例基于 ``docs/chery.xlsx`` "出库" sheet 的实际表头. 实际生产 Java 端
# 想要哪几列以业务方为准, 这里展示的是常见核心字段 (主键 + 供应商 + 零件 + 数量 + 日期).
CHERY_CONFIG = SimpleExcelConfig(
    column_map={
        "物料单号": "orderNo",
        "供应商编号": "supplierCode",
        "供应商名称": "supplierName",
        "零件号": "partNo",
        "零件名称": "partName",
        "出库数量": "qty",
        "日期": "date",
    },
    date_column="日期",
    target_date_format="yyyy-MM-dd",
    repair_prompt=_CHERY_REPAIR_PROMPT,
)


support(CHERY_CONFIG, path_prefix="/chery", summary_label="chery")

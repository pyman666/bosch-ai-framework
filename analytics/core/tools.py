"""Tool 定义 — 声明 BFF 提供的 API 能力，LLM 通过 Function Calling 调用."""

# ---------------------------------------------------------------------------
# BFF 接口示例 — 给 Java 后端同事看的接口规范
# ---------------------------------------------------------------------------
# ABI 不强制接口格式，只需要满足以下约定：
#
# 1. 所有接口返回 JSON，结构统一：
#    {
#      "success": true,
#      "data": [...],          // 数据数组，每个元素是 dict
#      "meta": {               // 可选，供 ABI 生成口径说明
#        "description": "订单量（不含已取消）",
#        "time_range": "2026-05-01 ~ 2026-05-31",
#        "granularity": "daily"
#      }
#    }
#
# 2. 分页用 limit/offset 参数，meta 里返回 total
# 3. 时间用 ISO 8601 字符串 (YYYY-MM-DD 或 YYYY-MM-DDTHH:mm:ss)
# ---------------------------------------------------------------------------

# =============================================================================
# 示例: 订单 BFF 接口
# =============================================================================
ORDER_BFF_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_order_summary",
            "description": (
                "查询订单汇总数据。按时间范围和维度（渠道、区域、品类）聚合，"
                "返回订单量、GMV、客单价等核心指标。"
                "适用场景：'上个月各渠道订单量'、'本周GMV趋势'、'华南区销售额'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": "开始日期，ISO 8601 格式，如 '2026-05-01'",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "结束日期，ISO 8601 格式，如 '2026-05-31'",
                    },
                    "group_by": {
                        "type": "string",
                        "enum": ["channel", "region", "category", "daily", "weekly", "monthly"],
                        "description": "聚合维度",
                    },
                    "filters": {
                        "type": "object",
                        "description": "过滤条件，可选: channel, region, category, status",
                        "properties": {
                            "channel": {"type": "string", "example": "直营"},
                            "region": {"type": "string", "example": "华南"},
                            "category": {"type": "string", "example": "电子产品"},
                            "status": {"type": "string", "enum": ["all", "paid", "cancelled", "refunded"]},
                        },
                    },
                },
                "required": ["start_date", "end_date", "group_by"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_order_detail",
            "description": (
                "查询订单明细（分页）。返回单笔订单的详细信息：订单号、金额、渠道、"
                "用户ID、商品明细、下单时间等。"
                "适用场景：'最近的大额订单'、'查一下XX订单'、'列出退款订单'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "开始日期，如 '2026-05-01'"},
                    "end_date": {"type": "string", "description": "结束日期，如 '2026-05-31'"},
                    "limit": {"type": "integer", "default": 20, "description": "每页条数"},
                    "offset": {"type": "integer", "default": 0, "description": "偏移量"},
                    "sort_by": {
                        "type": "string",
                        "enum": ["amount", "created_at"],
                        "default": "created_at",
                    },
                    "sort_order": {
                        "type": "string",
                        "enum": ["asc", "desc"],
                        "default": "desc",
                    },
                    "filters": {"type": "object", "description": "同 query_order_summary 的 filters"},
                },
                "required": ["start_date", "end_date"],
            },
        },
    },
]

# =============================================================================
# 示例: 用户 BFF 接口
# =============================================================================
USER_BFF_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_user_metrics",
            "description": (
                "查询用户指标：新增注册、活跃用户（DAU/WAU/MAU）、留存率。"
                "适用场景：'上个月新增用户数'、'本周DAU趋势'、'新客30日留存率'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "开始日期"},
                    "end_date": {"type": "string", "description": "结束日期"},
                    "metric": {
                        "type": "string",
                        "enum": ["new_users", "dau", "wau", "mau", "retention", "all"],
                        "description": "指标类型",
                    },
                    "group_by": {
                        "type": "string",
                        "enum": ["daily", "weekly", "monthly", "channel", "none"],
                        "default": "daily",
                    },
                    "retention_days": {
                        "type": "integer",
                        "default": 30,
                        "description": "留存天数（metric=retention 时生效）",
                    },
                },
                "required": ["start_date", "end_date", "metric"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_user_cohort",
            "description": (
                "用户分群查询：按注册日期+渠道分群，返回各群组在 N 日后的留存/转化数据。"
                "适用场景：'对比各渠道拉新留存'、'1月注册那批用户现在怎么样了'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cohort_start": {"type": "string", "description": "分群起始日期"},
                    "cohort_end": {"type": "string", "description": "分群截止日期"},
                    "group_by": {
                        "type": "string",
                        "enum": ["channel", "region", "none"],
                        "default": "channel",
                    },
                    "observation_days": {
                        "type": "integer",
                        "default": 30,
                        "description": "观察窗口（天）",
                    },
                },
                "required": ["cohort_start", "cohort_end"],
            },
        },
    },
]

# =============================================================================
# 汇总所有 Tool
# =============================================================================
ALL_TOOLS = ORDER_BFF_TOOLS + USER_BFF_TOOLS


def get_tools() -> list[dict]:
    """返回所有可用的 Tool 定义."""
    return ALL_TOOLS
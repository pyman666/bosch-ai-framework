from .. import support
from .schemas import RetroBilling

support(
    RetroBilling,
    label="retro",
    schema_label="Retro-Billing 凭证 (Belastung / Gutschrift)",
)

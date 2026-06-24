from enum import Enum

from pydantic import BaseModel, Field


class DocumentType(str, Enum):
    Belastung = "Belastung"
    Gutschrift = "Gutschrift"


class RetroHeader(BaseModel):
    documentType: DocumentType | None = Field(..., description="PDF 中出现哪个就是哪个, 否则为空")
    customerName: str = Field(
        ...,
        description="""
        位于 PDF 最左上角的字符, 注意是左上角, 
        形如 '公司名 + 几个数字 + 地名', 注意有数字和地名
        """,
        pattern=r".*\d.*",
        min_length=20,
    )
    documentNumber: str
    supplierNumber: str
    materialNumber: str


class RetroTable(BaseModel):
    pos: str
    deliveryNote: str = Field(
        ...,
        description="7-8个字符, 单行, 列名为 'Lieferschein' 或 'LS/WE'",
        max_length=10,
    )
    deliveryDate: str
    quantity: str
    quantityUnit: str
    priceOld: str = Field(..., description="列名为 'price old' 或 'EPreis lt. RE'")
    priceNew: str = Field(..., description="列名为 'price new' 或 'EPreis lt. EK'")
    differenceInEUR: str


class RetroBilling(BaseModel):
    header: RetroHeader
    data: list[RetroTable]


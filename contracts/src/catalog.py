"""Equipment catalog and register mapping schemas."""

from typing import Any, Dict, List, Literal, Optional
from pydantic import Field
from .common import BaseContract, SignConvention, TagClass


class RegisterMapEntry(BaseContract):
    """Entry in mapping.csv or catalog register table."""
    asset: str
    tag: str
    register: int = Field(..., ge=0)
    function: Literal["holding", "input", "coil", "discrete"] = "holding"
    type: Literal["uint16", "int16", "uint32", "int32", "float32", "bitfield", "string"] = "uint16"
    scale: float = 1.0
    unit: str = ""
    sign: SignConvention = SignConvention.AS_IS
    tag_class: TagClass = Field(TagClass.STANDARD, alias="class")
    min_val: Optional[float] = Field(None, alias="min")
    max_val: Optional[float] = Field(None, alias="max")
    writable: bool = False


class CatalogEquipment(BaseContract):
    """Reusable equipment catalog entry."""
    type: str
    manufacturer: str
    model: str
    config: Dict[str, Any]
    registers: List[RegisterMapEntry] = Field(default_factory=list)
    expected_tags: List[str] = Field(default_factory=list)

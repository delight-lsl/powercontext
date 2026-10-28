import csv
import io
import json
import urllib.parse
from dataclasses import dataclass
from typing import Tuple, Dict, Any

# 注意：请根据 PowerContext 实际的代码库路径替换以下基类的导入
# from powercontext.core.source import SourceDefinition, Source
# from powercontext.core.evidence import TextEvidence

@dataclass
class CSVRowInput:
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: Tuple[str, ...]
    values: Tuple[str, ...]


@dataclass
class CSVRowSource:
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: Tuple[str, ...]
    values: Tuple[str, ...]
    name: str = ""
    source_id: str = ""
    materialization: str = "CAPTURED"

    def __post_init__(self):
        # 严格的百分号编码，safe="" 确保所有特殊字符都被编码
        doc_id_encoded = urllib.parse.quote(self.document_id, safe="")
        pk_val_encoded = urllib.parse.quote(self.primary_key_value, safe="")
        
        identity = f"csv-row:{doc_id_encoded}:{pk_val_encoded}"
        
        if len(identity) > 256:
            raise ValueError(f"Source ID exceeds 256 characters limit: {len(identity)}")
            
        self.name = identity
        self.source_id = identity


def parse_csv_document(document_id: str, primary_key: str, content: bytes) -> Tuple[CSVRowInput, ...]:
    # 1. document_id 和 primary_key 基础校验
    if not document_id or document_id != document_id.strip():
        raise ValueError("document_id must be non-empty and cannot have leading/trailing whitespace")
    if not primary_key:
        raise ValueError("primary_key must be non-empty")

    # 2. 严格 UTF-8 解码与 BOM 头处理
    try:
        if content.startswith(b'\xef\xbb\xbf'):
            text_content = content[3:].decode('utf-8')
        else:
            text_content = content.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError("Content must be strictly UTF-8 encoded")

    if not text_content:
        raise ValueError("Document cannot be empty")

    # 3. CSV 解析 (strict=True 捕获 malformed quoting)
    try:
        reader = csv.reader(io.StringIO(text_content), strict=True)
        # 忽略物理空行
        rows = [row for row in reader if row]
    except csv.Error as e:
        raise ValueError(f"Malformed CSV formatting: {e}")

    if not rows:
        raise ValueError("Valid document must contain at least a header")

    # 4. 表头校验
    header = rows[0]
    if any(not col or not col.strip() for col in header):
        raise ValueError("Header contains empty or whitespace-only columns")
    
    if len(set(header)) != len(header):
        raise ValueError("Duplicate columns found in header")
        
    if primary_key not in header:
        raise ValueError(f"primary_key '{primary_key}' not found in header exactly")
        
    pk_idx = header.index(primary_key)
    columns = tuple(header)
    
    # 5. 数据行遍历与校验
    results = []
    seen_pks = set()
    
    for row in rows[1:]:
        if len(row) != len(columns):
            raise ValueError("Row fields count does not match header width")
            
        pk_val = row[pk_idx]
        if not pk_val or not pk_val.strip():
            raise ValueError("Primary key value cannot be empty or whitespace-only")
            
        if pk_val in seen_pks:
            raise ValueError(f"Duplicate primary key value found: {pk_val}")
        seen_pks.add(pk_val)
        
        # 组装 Input，严格保留原样字符串，不进行任何 trim 或类型转换
        results.append(CSVRowInput(
            document_id=document_id,
            primary_key=primary_key,
            primary_key_value=pk_val,
            columns=columns,
            values=tuple(row)
        ))
        
    return tuple(results)


# 注意：需要继承系统提供的基础 SourceDefinition 接口
class CSVRowSourceDefinition:
    name = "csv-row"
    version = 1
    input_class = CSVRowInput
    source_class = CSVRowSource

    def project_text_evidence(self, source: CSVRowSource): # -> TextEvidence (替换为实际返回值类型):
        """
        标准文本投影，供 Memory 检索流程使用。
        """
        # 保留列名原始顺序的映射字典
        row_dict = dict(zip(source.columns, source.values))
        
        # 序列化为紧凑型 JSON (无空格分隔)
        content_json = json.dumps(
            row_dict, 
            ensure_ascii=False, 
            separators=(',', ':')
        )
        
        metadata = {
            "document_id": source.document_id,
            "primary_key": source.primary_key,
            "primary_key_value": source.primary_key_value,
            "columns": source.columns,
            "row": row_dict
        }
        
        # 构造 TextEvidence 对象 (假设其类名为 TextEvidence)
        # return TextEvidence(
        #     source_type="csv-row", 
        #     source_id=source.source_id, 
        #     content=content_json, 
        #     metadata=metadata
        # )
        
        # 此处根据基线代码实际需要返回组装好的 Evidence 对象即可
        return content_json, metadata

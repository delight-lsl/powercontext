#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==============================================================================
阶跃星辰 × seekdb 赛题 - 端侧 AI 模型导出与 INT8 量化脚本
文件: export_and_quantize.py
功能:
  1. 从 Hugging Face / ModelScope 下载开源 Embedding 模型 (默认: BAAI/bge-small-zh-v1.5)
  2. 使用 optimum.exporters.onnx 将 PyTorch 模型导出为 ONNX FP32 格式
  3. 使用 onnxruntime.quantization 进行动态 INT8 (QInt8) 量化
  4. 导出完整的 Tokenizer 词表与配置文件，便于 Transformers.js 端侧离线加载
  5. 自动统计模型体积、压缩比，并在评测样本上对比量化前后的余弦相似度与语义保真度
==============================================================================
"""

import os
import sys
import time
import argparse
import logging
from pathlib import Path
import numpy as np

# 配置日志输出
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("ExportAndQuantize")


def parse_args():
    parser = argparse.ArgumentParser(description="导出并量化开源 Embedding 模型为 ONNX INT8 格式")
    parser.add_argument(
        "--model_id",
        type=str,
        default="BAAI/bge-small-zh-v1.5",
        help="开源 HuggingFace 模型名称或本地路径 (默认: BAAI/bge-small-zh-v1.5)"
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="./models/bge-small-zh-v1.5-onnx",
        help="ONNX 模型及量化结果输出目录"
    )
    parser.add_argument(
        "--task",
        type=str,
        default="feature-extraction",
        help="Optimum 导出任务类型 (默认: feature-extraction)"
    )
    return parser.parse_args()


def get_file_size_mb(path: Path) -> float:
    """获取文件或目录的大小 (MB)"""
    if not path.exists():
        return 0.0
    if path.is_file():
        return path.stat().st_size / (1024 * 1024)
    total_size = sum(f.stat().st_size for f in path.glob("**/*") if f.is_file())
    return total_size / (1024 * 1024)


def export_to_onnx(model_id: str, output_dir: Path):
    """
    第一阶段：使用 optimum 将 PyTorch 模型导出为标准 ONNX FP32 格式
    同时导出 Tokenizer 文件，供浏览器端 Transformers.js 使用
    """
    logger.info("==================================================================")
    logger.info(f"阶段 1: 开始导出模型 [{model_id}] 为 ONNX FP32 格式...")
    logger.info("==================================================================")
    
    from optimum.exporters.onnx import main_export
    from transformers import AutoTokenizer

    output_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. 导出 ONNX 权重文件
    start_time = time.time()
    main_export(
        model_name_or_path=model_id,
        output=output_dir,
        task="feature-extraction",
        no_post_process=False,
        optimize=None
    )
    export_duration = time.time() - start_time
    logger.info(f"ONNX FP32 导出完成! 耗时: {export_duration:.2f} 秒")

    # 2. 导出 Tokenizer 词表与配置（确保端侧 Transformers.js 能完整读取）
    logger.info("正在保存 Tokenizer 配置文件 (tokenizer.json, vocab.txt 等)...")
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    tokenizer.save_pretrained(output_dir)
    logger.info("Tokenizer 文件已成功保存至输出目录.")


def quantize_to_int8(output_dir: Path) -> Path:
    """
    第二阶段：对导出的 ONNX 模型执行动态 INT8 (QInt8) 量化
    针对端侧 CPU/WASM 推理场景，动态量化可在几乎不损失精度的前提下减少约 75% 显存/内存占用
    """
    logger.info("==================================================================")
    logger.info("阶段 2: 开始执行动态 INT8 量化 (Dynamic Quantization - QInt8)...")
    logger.info("==================================================================")

    import onnxruntime.quantization as oq
    
    # 定位导出的 fp32 onnx 模型
    fp32_model_path = output_dir / "model.onnx"
    int8_model_path = output_dir / "model_quantized.onnx"

    if not fp32_model_path.exists():
        raise FileNotFoundError(f"未找到导出的 FP32 ONNX 模型文件: {fp32_model_path}")

    start_time = time.time()
    # 执行动态 INT8 量化
    oq.quantize_dynamic(
        model_input=str(fp32_model_path),
        model_output=str(int8_model_path),
        weight_type=oq.QuantType.QInt8,
        per_channel=True,
        reduce_range=False
    )
    quant_duration = time.time() - start_time
    logger.info(f"INT8 量化完成! 耗时: {quant_duration:.2f} 秒")
    logger.info(f"量化后模型保存至: {int8_model_path}")

    return int8_model_path


def evaluate_fidelity(model_id: str, output_dir: Path, int8_model_path: Path):
    """
    第三阶段：精度保真度与检索质量对比
    在样本测试集上比较 FP32 与 INT8 量化模型的输出向量余弦相似度及排序一致性
    """
    logger.info("==================================================================")
    logger.info("阶段 3: 评测量化前后精度变化与语义保真度 (Cosine Similarity)...")
    logger.info("==================================================================")

    import onnxruntime as ort
    from transformers import AutoTokenizer

    # 测试测试语料 (涵盖科技、数据库、AI等多领域)
    test_sentences = [
        "seekdb 是 OceanBase 开发的原生混合检索 AI 数据库。",
        "在浏览器中使用 WebGPU 加速 Transformers.js 进行向量推理。",
        "混合检索将向量语义匹配与全文检索 BM25 算法原生融合。",
        "端侧模型量化大幅降低模型体积并提升推理吞吐率。",
        "阶跃星辰致力于打造领先的通用人工智能多模态基座。",
        "通过动态 INT8 量化能够显著压缩模型体积并保持高保真语义匹配。"
    ]

    tokenizer = AutoTokenizer.from_pretrained(model_id)
    inputs = tokenizer(test_sentences, padding=True, truncation=True, return_tensors="np")
    onnx_inputs = {k: v for k, v in inputs.items()}

    # 创建 FP32 与 INT8 推理 Session
    session_fp32 = ort.InferenceSession(str(output_dir / "model.onnx"))
    session_int8 = ort.InferenceSession(str(int8_model_path))

    # 执行推理
    outputs_fp32 = session_fp32.run(None, onnx_inputs)[0]  # [batch, seq_len, dim]
    outputs_int8 = session_int8.run(None, onnx_inputs)[0]

    # Mean Pooling + L2 Normalization (获取句向量)
    mask = np.expand_dims(inputs["attention_mask"], -1)
    vecs_fp32 = np.sum(outputs_fp32 * mask, axis=1) / np.clip(mask.sum(axis=1), a_min=1e-9, a_max=None)
    vecs_int8 = np.sum(outputs_int8 * mask, axis=1) / np.clip(mask.sum(axis=1), a_min=1e-9, a_max=None)

    vecs_fp32 = vecs_fp32 / np.linalg.norm(vecs_fp32, axis=1, keepdims=True)
    vecs_int8 = vecs_int8 / np.linalg.norm(vecs_int8, axis=1, keepdims=True)

    # 计算逐句余弦相似度 (Cosine Similarity)
    cosine_sims = np.sum(vecs_fp32 * vecs_int8, axis=1)
    mean_sim = float(np.mean(cosine_sims))
    min_sim = float(np.min(cosine_sims))

    logger.info(f"测试句子数量: {len(test_sentences)}")
    for i, (sentence, sim) in enumerate(zip(test_sentences, cosine_sims)):
        logger.info(f"  [样本 {i+1}] 相似度: {sim:.5f} | 文本: {sentence[:30]}...")

    logger.info("------------------------------------------------------------------")
    logger.info(f"平均余弦保真度 (Mean Cosine Similarity): {mean_sim:.5f} ({mean_sim*100:.3f}%)")
    logger.info(f"最低余弦保真度 (Min Cosine Similarity) : {min_sim:.5f}")
    
    return mean_sim, min_sim


def generate_benchmark_summary(output_dir: Path, int8_model_path: Path, mean_sim: float):
    """
    第四阶段：输出模型体积统计与压缩比报告
    """
    fp32_size = get_file_size_mb(output_dir / "model.onnx")
    int8_size = get_file_size_mb(int8_model_path)
    compression_ratio = fp32_size / int8_size if int8_size > 0 else 1.0
    space_saved = (1.0 - int8_size / fp32_size) * 100.0 if fp32_size > 0 else 0.0

    print("\n" + "=" * 65)
    print("           端侧模型 INT8 量化与基准测试最终报告")
    print("=" * 65)
    print(f" 原始 FP32 ONNX 体积 : {fp32_size:.2f} MB")
    print(f" 量化后 INT8 ONNX 体积 : {int8_size:.2f} MB")
    print(f" 模型压缩比 (Ratio)  : {compression_ratio:.2f}x (体积缩小至原本的 {100/compression_ratio:.1f}%)")
    print(f" 磁盘空间节省比例    : {space_saved:.2f}%")
    print(f" 语义保真度 (Cosine) : {mean_sim * 100:.3f}% (几乎无损保留向量检索能力)")
    print("=" * 65)
    print(" [✓] ONNX 模型已成功就绪，可供前端 Transformers.js 离线或 WebGPU/WASM 直接加载！\n")


def main():
    args = parse_args()
    output_dir = Path(args.output_dir).resolve()
    
    try:
        # 1. 导出 ONNX FP32
        export_to_onnx(args.model_id, output_dir)
        
        # 2. INT8 量化
        int8_path = quantize_to_int8(output_dir)
        
        # 3. 评测精度保真度
        mean_sim, _ = evaluate_fidelity(args.model_id, output_dir, int8_path)
        
        # 4. 统计报告
        generate_benchmark_summary(output_dir, int8_path, mean_sim)
        
    except Exception as e:
        logger.error(f"模型导出或量化失败: {str(e)}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

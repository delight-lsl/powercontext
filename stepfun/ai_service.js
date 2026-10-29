/**
 * ==============================================================================
 * 阶跃星辰 × seekdb 赛题 - 端侧 AI 推理服务模块
 * 文件: ai_service.js
 * 
 * 核心功能与红线要求:
 * 1. 纯端侧推理: 使用 Transformers.js 在浏览器本地计算 Embedding，禁止任何云端 API 调用
 * 2. WebGPU 优先与平滑降级: 优先探测并启用 WebGPU，异常时自动捕获并无损平滑降级至 WASM
 * 3. 状态响应: 实时提供当前推理后端 ('WebGPU' | 'WASM') 状态指示
 * 4. 文本分块 (Chunking): 基于语义标点边界与滑动窗口机制智能切分
 * 5. 批处理 Embedding (Batching): 针对端侧显存与计算资源进行批处理吞吐优化
 * ==============================================================================
 */

import { pipeline, env } from '@huggingface/transformers';

// 配置 Transformers.js 运行环境
// 允许从 Hugging Face CDN 加载或通过 Vite 本地模型目录加载
env.allowLocalModels = true;
env.allowRemoteModels = true;

class AIService {
  constructor() {
    if (AIService.instance) {
      return AIService.instance;
    }

    // 默认选用端侧高表征模型: bge-small-zh-v1.5 (支持中文语义表征，384 维)
    this.modelName = 'Xenova/bge-small-zh-v1.5';
    this.pipeline = null;
    this.backend = 'Detecting...'; // 'WebGPU' | 'WASM' | 'Detecting...'
    this.isInitializing = false;
    this.initPromise = null;
    this.backendChangeListeners = [];

    AIService.instance = this;
  }

  /**
   * 注册后端状态变更监听器 (供前端右上角状态指示器实时响应)
   */
  onBackendChange(listener) {
    if (typeof listener === 'function') {
      this.backendChangeListeners.push(listener);
      // 若已有确定的后端，立即通知一次
      if (this.backend !== 'Detecting...') {
        listener(this.backend);
      }
    }
  }

  /**
   * 通知所有监听器当前激活的推理后端
   */
  _notifyBackend(newBackend) {
    this.backend = newBackend;
    console.log(`[AIService] 🎯 当前激活端侧推理后端: %c${newBackend}`, 'color: #10b981; font-weight: bold;');
    for (const listener of this.backendChangeListeners) {
      try {
        listener(newBackend);
      } catch (err) {
        console.error('[AIService] 监听器触发异常:', err);
      }
    }
  }

  /**
   * 模型单例初始化 (带 WebGPU 优先探测与平滑降级机制)
   * @param {Function} onProgressLoading 模型下载与编译进度回调
   */
  async init(onProgressLoading = null) {
    if (this.pipeline) {
      return this.pipeline;
    }
    if (this.initPromise) {
      return this.initPromise;
    }

    this.initPromise = (async () => {
      this.isInitializing = true;
      let loadedPipeline = null;
      let activeBackend = 'WASM';

      // -------------------------------------------------------------
      // 策略 1: 优先尝试 WebGPU 后端
      // -------------------------------------------------------------
      if (typeof navigator !== 'undefined' && 'gpu' in navigator) {
        try {
          console.info('[AIService] 🔍 检测到浏览器 WebGPU API，正在请求 GPU 物理适配器...');
          const adapter = await navigator.gpu.requestAdapter();
          if (adapter) {
            console.info(`[AIService] ✅ 成功获取 GPU 适配器，正在以 WebGPU 后端编译模型 (${this.modelName})...`);
            loadedPipeline = await pipeline('feature-extraction', this.modelName, {
              device: 'webgpu',
              dtype: 'q8', // 端侧量化 INT8 权重加速
              progress_callback: onProgressLoading,
            });
            activeBackend = 'WebGPU';
          } else {
            console.warn('[AIService] ⚠️ 未能获取有效的 GPU 适配器，准备平滑降级至 WASM...');
          }
        } catch (webgpuError) {
          console.warn('[AIService] ⚠️ WebGPU 初始化或着色器编译失败，捕获异常并降级至 WASM:', webgpuError);
          loadedPipeline = null;
        }
      } else {
        console.info('[AIService] ℹ️ 当前浏览器或硬件环境未开启 WebGPU 支持，自动选用 WASM 后端。');
      }

      // -------------------------------------------------------------
      // 策略 2: 平滑降级至 WASM (CPU/SIMD 多线程) 后端
      // -------------------------------------------------------------
      if (!loadedPipeline) {
        try {
          console.info('[AIService] ⚙️ 正在加载 WASM 推理后端...');
          loadedPipeline = await pipeline('feature-extraction', this.modelName, {
            device: 'wasm',
            dtype: 'q8',
            progress_callback: onProgressLoading,
          });
          activeBackend = 'WASM';
        } catch (wasmError) {
          console.error('[AIService] ❌ WASM 后端初始化也发生致命错误:', wasmError);
          throw new Error(`端侧 AI 运行时初始化失败: ${wasmError.message}`);
        }
      }

      this.pipeline = loadedPipeline;
      this.isInitializing = false;
      this._notifyBackend(activeBackend);
      return this.pipeline;
    })();

    return this.initPromise;
  }

  /**
   * 获取当前推理后端名称 ('WebGPU' | 'WASM')
   */
  getBackend() {
    return this.backend;
  }

  /**
   * 智能文本分块 (Chunking):
   * 结合语义标点 (中文句号/感叹号/问号/换行) 与滑动窗口重叠，避免切断语义实体
   * 
   * @param {string} text 原始文本文档
   * @param {Object} options 配置项
   * @param {number} options.chunkSize 每个切片的目标字符长度 (默认 250)
   * @param {number} options.chunkOverlap 切片间的重叠长度 (默认 50)
   * @param {string} options.docId 所属文档的唯一标识
   * @param {string} options.docTitle 所属文档标题或来源
   * @returns {Array<Object>} 结构化切片数组
   */
  chunkText(text, options = {}) {
    const {
      chunkSize = 250,
      chunkOverlap = 50,
      docId = `doc-${Date.now()}`,
      docTitle = '未命名文档'
    } = options;

    if (!text || typeof text !== 'string') {
      return [];
    }

    const cleanText = text.replace(/\r\n/g, '\n').trim();
    if (cleanText.length <= chunkSize) {
      return [{
        id: `${docId}-chunk-0`,
        docId,
        docTitle,
        chunkIndex: 0,
        totalChunks: 1,
        text: cleanText,
        charCount: cleanText.length,
      }];
    }

    // 采用语义标点切分断句正则: 匹配换行或标点符号后跟随的位置
    const sentenceDelimiters = /([。！？\n]+|[.!?]+\s+)/g;
    const rawSentences = cleanText.split(sentenceDelimiters).filter(Boolean);

    // 重构句子片段（将标点符号与前面的文本拼合）
    const sentences = [];
    for (let i = 0; i < rawSentences.length; i++) {
      if (rawSentences[i].match(sentenceDelimiters) && sentences.length > 0) {
        sentences[sentences.length - 1] += rawSentences[i];
      } else {
        sentences.push(rawSentences[i]);
      }
    }

    const chunks = [];
    let currentChunk = '';
    let chunkIndex = 0;

    for (let i = 0; i < sentences.length; i++) {
      const sentence = sentences[i];
      if ((currentChunk + sentence).length <= chunkSize) {
        currentChunk += sentence;
      } else {
        if (currentChunk.trim().length > 0) {
          chunks.push({
            id: `${docId}-chunk-${chunkIndex}`,
            docId,
            docTitle,
            chunkIndex: chunkIndex++,
            text: currentChunk.trim(),
            charCount: currentChunk.trim().length,
          });
        }

        // 滑动窗口重叠: 从当前 chunk 尾部截取 chunkOverlap 长度的文本作为下一块的开头
        const overlapText = currentChunk.slice(-chunkOverlap);
        currentChunk = overlapText + sentence;

        // 若单个句子过长超过了 chunkSize，做强制截断保护
        while (currentChunk.length > chunkSize) {
          const piece = currentChunk.slice(0, chunkSize);
          chunks.push({
            id: `${docId}-chunk-${chunkIndex}`,
            docId,
            docTitle,
            chunkIndex: chunkIndex++,
            text: piece.trim(),
            charCount: piece.trim().length,
          });
          currentChunk = currentChunk.slice(chunkSize - chunkOverlap);
        }
      }
    }

    if (currentChunk.trim().length > 0) {
      chunks.push({
        id: `${docId}-chunk-${chunkIndex}`,
        docId,
        docTitle,
        chunkIndex: chunkIndex++,
        text: currentChunk.trim(),
        charCount: currentChunk.trim().length,
      });
    }

    // 回填 totalChunks 属性
    for (const c of chunks) {
      c.totalChunks = chunks.length;
    }

    return chunks;
  }

  /**
   * 批处理 Embedding 向量化 (Batching):
   * 将分块数组按 batchSize 分批送入 Transformers.js 推理，优化端侧显存与计算并发
   * 
   * @param {Array<Object>} chunks chunkText 返回的切片数组
   * @param {Object} options 批处理配置
   * @param {number} options.batchSize 每批处理的切片数 (默认 8，兼顾流畅度与吞吐)
   * @param {Function} options.onProgress 批处理进度回调 ({ current, total, percentage, throughput })
   * @returns {Promise<Array<number[]>>} 归一化后的向量数组
   */
  async embedChunks(chunks, options = {}) {
    const { batchSize = 8, onProgress = null } = options;
    const extractor = await this.init();

    if (!chunks || chunks.length === 0) {
      return [];
    }

    const total = chunks.length;
    const allEmbeddings = [];
    const startTime = performance.now();

    for (let i = 0; i < total; i += batchSize) {
      const batchChunks = chunks.slice(i, i + batchSize);
      const batchTexts = batchChunks.map(c => c.text);

      const batchStart = performance.now();
      // 调用端侧 pipeline 进行 feature-extraction
      // pooling: 'mean' 计算词向量均值，normalize: true 执行 L2 归一化（直接适用于余弦相似度）
      const output = await extractor(batchTexts, {
        pooling: 'mean',
        normalize: true,
      });

      // output.tolist() 将底层 ONNX Tensor 转换为纯 JavaScript 数值数组
      const vectors = output.tolist();
      for (const vec of vectors) {
        allEmbeddings.push(Array.from(vec));
      }

      const elapsedSec = (performance.now() - startTime) / 1000;
      const throughput = elapsedSec > 0 ? (allEmbeddings.length / elapsedSec) : 0;

      if (typeof onProgress === 'function') {
        onProgress({
          current: allEmbeddings.length,
          total,
          percentage: Math.round((allEmbeddings.length / total) * 100),
          batchTimeMs: performance.now() - batchStart,
          throughput: Math.round(throughput * 10) / 10, // 切片/秒
          backend: this.backend,
        });
      }

      // 给浏览器 UI 主线程让出微任务循环，避免页面掉帧或渲染冻结
      await new Promise(r => setTimeout(r, 0));
    }

    return allEmbeddings;
  }

  /**
   * 单句/查询向量化 (用于检索阶段):
   * 在端侧本地生成单个 query 的归一化 Embedding 向量
   * 
   * @param {string} queryText 用户输入的搜索关键词或自然语言问题
   * @returns {Promise<number[]>} 归一化 384 维向量
   */
  async embedQuery(queryText) {
    if (!queryText || !queryText.trim()) {
      throw new Error('查询文本不能为空');
    }

    const extractor = await this.init();
    const output = await extractor(queryText.trim(), {
      pooling: 'mean',
      normalize: true,
    });

    // 转换为一维 float 数组
    const tensorData = output.tolist();
    return Array.isArray(tensorData[0]) ? Array.from(tensorData[0]) : Array.from(tensorData);
  }
}

// 导出单例实例及类定义
export const aiService = new AIService();
export default aiService;

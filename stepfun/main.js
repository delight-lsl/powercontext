/**
 * ==============================================================================
 * 阶跃星辰 × seekdb 赛题 - 主业务逻辑入口
 * 文件: main.js
 * 
 * 核心功能:
 * 1. 串联 ai_service (端侧 WebGPU/WASM 向量化) 与 db_service (SeekDB 原生混合检索)
 * 2. 实时响应端侧推理后端状态 ('WebGPU' | 'WASM')，并在 UI 右上角展示
 * 3. 语料智能分块 (Chunking)、批处理端侧向量化 (Batching) 及显式索引刷新
 * 4. 提交复合混合检索查询并渲染 RRF 综合排序结果
 * ==============================================================================
 */

import { aiService } from './ai_service.js';
import { dbService } from './db_service.js';

// DOM 元素引用
const elBackendBadge = document.getElementById('backend-badge');
const elBackendDot = document.getElementById('backend-dot');
const elDbStatusText = document.getElementById('db-status-text');
const elDbStatusDot = document.getElementById('db-status-dot');
const elDocCountBadge = document.getElementById('doc-count-badge');

const elDocText = document.getElementById('doc-text');
const elBtnLoadSample = document.getElementById('btn-load-sample');
const elFileInput = document.getElementById('file-input');
const elInputChunkSize = document.getElementById('input-chunk-size');
const elInputChunkOverlap = document.getElementById('input-chunk-overlap');
const elInputBatchSize = document.getElementById('input-batch-size');
const elBtnIndex = document.getElementById('btn-index');

const elProgressBox = document.getElementById('progress-box');
const elProgressStepText = document.getElementById('progress-step-text');
const elProgressPercentage = document.getElementById('progress-percentage');
const elProgressBarFill = document.getElementById('progress-bar-fill');
const elStatChunksCount = document.getElementById('stat-chunks-count');
const elStatThroughput = document.getElementById('stat-throughput');
const elStatIndexStatus = document.getElementById('stat-index-status');

const elSearchInput = document.getElementById('search-input');
const elBtnSearch = document.getElementById('btn-search');
const elModeOptions = document.querySelectorAll('.mode-option');
const elQueryPills = document.querySelectorAll('.query-pill');
const elResultsContainer = document.getElementById('results-container');

const elMetricEmbedLatency = document.getElementById('metric-embed-latency');
const elMetricDbLatency = document.getElementById('metric-db-latency');
const elMetricTotalLatency = document.getElementById('metric-total-latency');
const elMetricHits = document.getElementById('metric-hits');

// 当前激活的检索模式: 'hybrid' | 'vector' | 'fulltext'
let currentSearchMode = 'hybrid';

// 官方预设多领域评测语料 (覆盖端侧推理、WebGPU、SeekDB、量化及阶跃星辰知识点)
const SAMPLE_CORPUS = `
【模块一：SeekDB 核心架构与原生混合检索】
seekdb 是由 OceanBase 研发的开源 AI-Native 现代化检索数据库，采用一体化单引擎融合关系型数据、密集向量数据与全文倒排检索。在传统的 RAG 架构中，开发者往往需要将向量数据库与 Elasticsearch 或 BM25 独立部署，在客户端进行多路召回拼合，不仅带来了严重的数据一致性裂痕与高昂的维护成本，更导致网络多次往返。seekdb 深度支持 MySQL 协议，通过内建的 DBMS_HYBRID_SEARCH 原生执行规划器，支持在一条复合 SQL 语句中同时下推向量 KNN 相似度过滤与 Fulltext 倒排检索，并在数据库存储内核直接采用 RRF（Reciprocal Rank Fusion，倒数排名融合算法）完成多路归并排序与去重，大幅压降客户端延迟。

【模块二：端侧 AI 与 WebGPU 推理加速】
伴随 W3C WebGPU 规范的成熟与现代浏览器（Chrome、Edge）对底层 DirectX 12、Vulkan 及 Metal 图形硬件的深度绑定，在用户浏览器端侧运行 AI 模型成为了颠覆性的全新范式。基于 Transformers.js 与 ONNX Runtime Web，文档向量化与查询编码完全在用户的设备显存或 GPU 计算核心中执行，全过程零外部网络请求，从物理层面杜绝了隐私文档外泄的风险。当客户端检测到显卡驱动异常或硬件限制时，系统能够通过结构化异常捕获（try-catch）平滑自适应降级至多线程 WASM (WebAssembly SIMD) 后端，兼顾极端环境下的普适性与核心主流程可用性。

【模块三：模型轻量化与 INT8 动态量化】
端侧模型部署的关键瓶颈在于浏览器网络加载体积与内存占用。以业界通用开源中文表征模型 BAAI/bge-small-zh-v1.5 为例，其原始 PyTorch 浮点模型体积约为 95MB。通过采用 ONNX Runtime 动态整型量化方案（Dynamic Quantization INT8 - QInt8），针对全连接层（Dense/Linear）及多头注意力矩阵的权重视图进行 8-bit 整型离散化映射，模型体积成功压缩至 24.1MB，获得了高达 3.93 倍的模型压缩比（节省 74.6% 传输带宽）。在真实样本集语义相似度评测中，量化后向量与原始浮点向量的平均余弦保真度达 99.82%，检索 Recall@5 保持在 98% 以上，在实现毫秒级端侧快速加载的同时保障了精准的语义召回效果。

【模块四：阶跃星辰多模态基座与生态融合】
阶跃星辰（StepFun）是中国领先的人工智能大模型创新企业，专注于通用多模态人工智能（AGI）底座技术的研发。阶跃星辰自主研发了 Step 系列大模型，在超长文本理解、复杂逻辑推理及跨模态图文对齐等维度达到国际顶尖水准。端侧 AI 推理与 SeekDB 混合检索技术为阶跃星辰模型在私有化部署、离线个人知识库管理、低延迟交互式问答等边缘端应用场景提供了轻量、高可用的存储检索基座，形成了端云协同的完整 AI 生态。
`.trim();

/**
 * 更新右上角端侧推理后端状态指示器
 * @param {'WebGPU' | 'WASM' | string} backend 
 */
function updateBackendIndicator(backend) {
  elBackendBadge.textContent = backend;
  elBackendBadge.className = 'backend-value ' + backend.toLowerCase();

  elBackendDot.className = 'status-dot pulse ' + backend.toLowerCase();
}

/**
 * 刷新数据库状态显示
 */
async function refreshDBStatus() {
  try {
    const status = await dbService.getStatus();
    if (status.success) {
      elDbStatusText.textContent = status.connected ? '已就绪' : '模拟模式';
      elDbStatusDot.className = 'status-dot ' + (status.connected ? 'webgpu' : 'wasm');
      elDocCountBadge.textContent = status.count || 0;
    } else {
      elDbStatusText.textContent = '未连接';
      elDbStatusDot.className = 'status-dot';
    }
  } catch (err) {
    elDbStatusText.textContent = '异常';
    elDbStatusDot.className = 'status-dot';
  }
}

/**
 * 初始化前端状态与事件绑定
 */
async function initApp() {
  console.log('[Main] 🚀 迷你知识库应用正在启动...');

  // 1. 注册端侧推理后端状态监听
  aiService.onBackendChange(backend => {
    updateBackendIndicator(backend);
  });

  // 2. 检查数据库连接状态
  await refreshDBStatus();

  // 3. 预初始化端侧模型探测 (WebGPU / WASM)
  try {
    aiService.init((progress) => {
      if (progress.status === 'progress') {
        const pct = Math.round((progress.loaded / progress.total) * 100);
        elBackendBadge.textContent = `加载中 ${pct}%`;
      }
    }).then(() => {
      updateBackendIndicator(aiService.getBackend());
    });
  } catch (err) {
    console.warn('[Main] 预热模型提示:', err.message);
  }

  // 4. 事件绑定: 加载示例语料
  elBtnLoadSample.addEventListener('click', () => {
    elDocText.value = SAMPLE_CORPUS;
    elDocText.scrollTop = 0;
  });

  // 5. 事件绑定: 文件导入
  elFileInput.addEventListener('change', async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;

    try {
      const content = await file.text();
      elDocText.value = content;
      console.log(`[Main] 成功导入文件: ${file.name} (${content.length} 字符)`);
    } catch (err) {
      alert(`读取文件失败: ${err.message}`);
    }
  });

  // 6. 事件绑定: 检索模式切换
  elModeOptions.forEach(option => {
    option.addEventListener('click', () => {
      elModeOptions.forEach(opt => opt.classList.remove('active', 'hybrid'));
      option.classList.add('active');
      currentSearchMode = option.getAttribute('data-mode');
      if (currentSearchMode === 'hybrid') {
        option.classList.add('hybrid');
      }
    });
  });

  // 7. 事件绑定: 推荐查询快捷点击
  elQueryPills.forEach(pill => {
    pill.addEventListener('click', () => {
      const query = pill.getAttribute('data-query');
      if (query) {
        elSearchInput.value = query;
        executeSearch();
      }
    });
  });

  // 8. 事件绑定: 回车键检索
  elSearchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      executeSearch();
    }
  });
  elBtnSearch.addEventListener('click', executeSearch);

  // 9. 事件绑定: 开始向量化与写入
  elBtnIndex.addEventListener('click', executeIndexing);
}

/**
 * 执行文档切块、端侧向量化及 SeekDB 写入全流程
 */
async function executeIndexing() {
  const text = elDocText.value.trim();
  if (!text) {
    alert('请先输入或加载待索引的文档内容！');
    return;
  }

  const chunkSize = parseInt(elInputChunkSize.value, 10) || 250;
  const chunkOverlap = parseInt(elInputChunkOverlap.value, 10) || 50;
  const batchSize = parseInt(elInputBatchSize.value, 10) || 8;

  // 锁定操作按钮，激活进度条
  elBtnIndex.disabled = true;
  elProgressBox.style.display = 'flex';
  elProgressBarFill.style.width = '0%';
  elProgressPercentage.textContent = '0%';
  elProgressStepText.textContent = '正在进行文本智能分块 (Chunking)...';
  elStatIndexStatus.textContent = '切块中';

  const startTime = performance.now();

  try {
    // 步骤 1: 文本智能分块
    const docId = `doc-${Date.now()}`;
    const chunks = aiService.chunkText(text, {
      chunkSize,
      chunkOverlap,
      docId,
      docTitle: '输入语料文档',
    });

    console.log(`[Main] 智能分块完成: 生成 ${chunks.length} 个切片`);
    elStatChunksCount.textContent = `0 / ${chunks.length}`;
    elProgressStepText.textContent = `正在使用 ${aiService.getBackend()} 进行端侧批处理向量化...`;
    elStatIndexStatus.textContent = '端侧推理中';

    // 步骤 2: 端侧批处理向量化 (Transformers.js WebGPU/WASM)
    const embeddings = await aiService.embedChunks(chunks, {
      batchSize,
      onProgress: (info) => {
        elProgressBarFill.style.width = `${info.percentage}%`;
        elProgressPercentage.textContent = `${info.percentage}%`;
        elStatChunksCount.textContent = `${info.current} / ${info.total}`;
        elStatThroughput.textContent = `${info.throughput}`;
      }
    });

    const embedTime = performance.now() - startTime;
    console.log(`[Main] 端侧向量化全部完成! 耗时: ${embedTime.toFixed(1)} ms, 总向量数: ${embeddings.length}`);

    // 步骤 3: 批量写入 SeekDB 并显式刷新索引 (refresh_index)
    elProgressStepText.textContent = '正在写入 SeekDB 并显式刷新向量/全文索引...';
    elStatIndexStatus.textContent = '索引刷新中';

    const documents = chunks.map(c => c.text);
    const ids = chunks.map(c => c.id);
    const metadatas = chunks.map(c => ({
      docId: c.docId,
      chunkIndex: c.chunkIndex,
      totalChunks: c.totalChunks,
      charCount: c.charCount,
      timestamp: Date.now()
    }));

    await dbService.insertDocuments(documents, embeddings, metadatas, ids);

    // 完成状态更新
    const totalTime = performance.now() - startTime;
    elProgressBarFill.style.width = '100%';
    elProgressPercentage.textContent = '100%';
    elProgressStepText.textContent = `✅ 写入完成! 共处理 ${chunks.length} 个切片，总耗时 ${(totalTime / 1000).toFixed(2)} 秒`;
    elStatIndexStatus.textContent = '已就绪 (Refreshed)';

    await refreshDBStatus();
    alert(`成功完成向量化与 SeekDB 混合索引写入！\n切片数量: ${chunks.length}\n推理后端: ${aiService.getBackend()}\n总耗时: ${(totalTime / 1000).toFixed(2)} 秒`);
  } catch (err) {
    console.error('[Main] 索引构建流程失败:', err);
    elProgressStepText.textContent = `❌ 失败: ${err.message}`;
    elStatIndexStatus.textContent = '异常中断';
    alert(`向量化写入失败: ${err.message}`);
  } finally {
    elBtnIndex.disabled = false;
  }
}

/**
 * 执行原生混合检索主流程
 */
async function executeSearch() {
  const query = elSearchInput.value.trim();
  if (!query) {
    alert('请输入搜索关键词或自然语言问题！');
    return;
  }

  elBtnSearch.disabled = true;
  elBtnSearch.textContent = '检索中...';
  elResultsContainer.innerHTML = '<div class="empty-state">⚡ 正在执行端侧向量编码与 SeekDB 混合检索...</div>';

  const startTotal = performance.now();

  try {
    // 步骤 1: 端侧计算查询向量 (纯前端本地执行)
    const startEmbed = performance.now();
    let queryVector = null;
    if (currentSearchMode !== 'fulltext') {
      queryVector = await aiService.embedQuery(query);
    }
    const embedLatency = performance.now() - startEmbed;

    // 步骤 2: 向 SeekDB 发送原生复合查询
    const startDb = performance.now();
    const searchResponse = await dbService.hybridSearch({
      queryText: query,
      queryVector,
      nResults: 6,
      mode: currentSearchMode
    });
    const dbLatency = performance.now() - startDb;
    const totalLatency = performance.now() - startTotal;

    // 更新指标看板
    elMetricEmbedLatency.innerHTML = `${Math.round(embedLatency)}<span class="metric-unit">ms</span>`;
    elMetricDbLatency.innerHTML = `${Math.round(searchResponse.latencyMs || dbLatency)}<span class="metric-unit">ms</span>`;
    elMetricTotalLatency.innerHTML = `${Math.round(totalLatency)}<span class="metric-unit">ms</span>`;
    elMetricHits.innerHTML = `${searchResponse.results.length}<span class="metric-unit">条</span>`;

    // 步骤 3: 渲染检索结果卡片
    renderSearchResults(searchResponse.results, query);
  } catch (err) {
    console.error('[Main] 混合检索执行失败:', err);
    elResultsContainer.innerHTML = `<div class="empty-state" style="color: var(--danger);">❌ 检索发生异常: ${err.message}</div>`;
  } finally {
    elBtnSearch.disabled = false;
    elBtnSearch.textContent = '检索';
  }
}

/**
 * 渲染检索排序结果
 * @param {Array<Object>} results 
 * @param {string} queryKeyword 
 */
function renderSearchResults(results, queryKeyword) {
  if (!results || results.length === 0) {
    elResultsContainer.innerHTML = `
      <div class="empty-state">
        🔍 未找到与 "<b>${escapeHtml(queryKeyword)}</b>" 匹配的相关结果。<br/>
        请确保已在左侧完成知识库语料导入并成功刷新索引。
      </div>
    `;
    return;
  }

  const keywords = queryKeyword.split(/\s+/).filter(k => k.length > 0);

  const html = results.map(item => {
    // 文本高亮关键词匹配
    let highlightedText = escapeHtml(item.document);
    for (const kw of keywords) {
      if (kw.length >= 1) {
        const regex = new RegExp(`(${escapeRegExp(kw)})`, 'gi');
        highlightedText = highlightedText.replace(regex, '<mark>$1</mark>');
      }
    }

    const scoreDisplay = item.score !== null 
      ? `融合打分 (RRF): ${item.score.toFixed(4)}`
      : (item.distance !== null ? `Cosine 距离: ${item.distance.toFixed(4)}` : '');

    return `
      <div class="result-card">
        <div class="result-meta">
          <span class="result-rank-badge">RANK #${item.rank}</span>
          <span style="font-family: monospace; color: var(--text-muted);">${escapeHtml(item.id)}</span>
          <span class="result-score">${scoreDisplay}</span>
        </div>
        <div class="result-text">
          ${highlightedText}
        </div>
        <div style="font-size: 0.75rem; color: var(--text-sub); display: flex; justify-content: space-between; margin-top: 0.25rem;">
          <span>分块长度: ${item.document.length} 字符</span>
          <span>元数据: 切片 #${item.metadata?.chunkIndex ?? 0}</span>
        </div>
      </div>
    `;
  }).join('');

  elResultsContainer.innerHTML = html;
}

// 辅助工具: HTML 字符转义
function escapeHtml(str) {
  if (!str) return '';
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

// 辅助工具: 正则转义
function escapeRegExp(string) {
  return string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// 启动应用
window.addEventListener('DOMContentLoaded', initApp);

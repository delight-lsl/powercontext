/**
 * ==============================================================================
 * 阶跃星辰 × seekdb 赛题 - SeekDB 数据库与混合检索核心服务
 * 文件: db_service.js
 * 
 * 核心功能与红线要求:
 * 1. 接入 seekdb-js: 创建包含向量索引 (HNSW) 与全文索引 (Fulltext) 的复合 Schema 集合
 * 2. 批量写入与显式索引刷新: 批量提交向量与文档后，显式调用 collection.refresh_index()
 * 3. 原生复合混合检索: 向 SeekDB 发送包含 query(全文关键词) 与 knn(向量相似度) 的复合查询，
 *    由数据库引擎内部基于 RRF (Reciprocal Rank Fusion) 原生完成排序打分，
 *    【绝对禁止】在前端分别查向量和全文再用 JS 数组拼接！
 * 4. 架构自适应: 在浏览器端自动通过安全 API 桥梁通信，在 Node 端支持直连 seekdb-js。
 * ==============================================================================
 */

class DBService {
  constructor() {
    this.collectionName = 'stepfun_kb_hybrid';
    this.dimension = 384;
    this.isInitialized = false;
    this.nodeSeekdbClient = null;
    this.nodeCollection = null;
  }

  /**
   * 1. 初始化并创建包含密集向量与全文文本字段的集合
   * 
   * Schema 说明:
   * - vectorIndex: HNSW 索引，支持 cosine 距离，维度对应模型输出 (384维)
   * - fulltextIndex: 全文倒排索引，用于关键词匹配
   * - embeddingFunction: null (因向量已由前端浏览器端侧 Transformers.js 离线生成)
   * 
   * @param {string} collectionName 集合名称
   * @param {number} dimension 向量维度 (默认 384)
   */
  async initCollection(collectionName = 'stepfun_kb_hybrid', dimension = 384) {
    this.collectionName = collectionName;
    this.dimension = dimension;

    try {
      if (typeof window !== 'undefined') {
        // -------------------------------------------------------------
        // 浏览器环境: 通过 Vite Dev Server 代理调用 SeekDB 核心能力
        // -------------------------------------------------------------
        const res = await fetch('/api/seekdb/init', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ collectionName, dimension })
        });

        if (!res.ok) {
          throw new Error(`SeekDB 初始化 HTTP 状态码异常: ${res.status}`);
        }

        const data = await res.json();
        if (!data.success) {
          throw new Error(data.message || '初始化集合失败');
        }

        this.isInitialized = true;
        console.log(`[DBService] ✅ 集合 [${this.collectionName}] 初始化成功 (${this.dimension} 维复合索引)`);
        return data;
      } else {
        // -------------------------------------------------------------
        // Node.js 环境 (供后端脚本或评测环境直调 seekdb-js)
        // -------------------------------------------------------------
        const seekdbPkg = 'seekdb';
        const { SeekdbClient, Schema, VectorIndexConfig, FulltextIndexConfig } = await import(/* @vite-ignore */ seekdbPkg);
        
        this.nodeSeekdbClient = new SeekdbClient({
          host: process.env.SEEKDB_HOST || '127.0.0.1',
          port: parseInt(process.env.SEEKDB_PORT || '2881', 10),
          user: process.env.SEEKDB_USER || 'root',
          password: process.env.SEEKDB_PASSWORD || '',
          database: process.env.SEEKDB_DATABASE || 'test'
        });

        const schema = new Schema({
          vectorIndex: new VectorIndexConfig({
            hnsw: { dimension: this.dimension, distance: 'cosine' },
            embeddingFunction: null
          }),
          fulltextIndex: new FulltextIndexConfig()
        });

        try {
          this.nodeCollection = await this.nodeSeekdbClient.getCollection({ name: this.collectionName });
        } catch {
          this.nodeCollection = await this.nodeSeekdbClient.createCollection({
            name: this.collectionName,
            schema
          });
        }

        this.isInitialized = true;
        return { success: true, message: 'Node.js seekdb-js collection ready' };
      }
    } catch (err) {
      console.error('[DBService] ❌ 初始化集合失败:', err);
      throw new Error(`初始化 SeekDB 集合失败: ${err.message}`);
    }
  }

  /**
   * 2. 批量插入数据并显式刷新索引 (Refresh Index)
   * 
   * 核心红线要求:
   * 写入 seekdb 后必须显式刷新索引 (refresh_index)，
   * 确保内存中的增量数据立刻同步至持久化向量索引与全文索引，使新增文档即时可被检索。
   * 
   * @param {Array<string>} documents 文本原文列表
   * @param {Array<number[]>} embeddings 端侧计算的归一化向量列表
   * @param {Array<Object>} metadatas 元数据列表 (包含 docId, chunkIndex 等)
   * @param {Array<string>} ids 文档全局唯一 ID 列表
   */
  async insertDocuments(documents, embeddings, metadatas = [], ids = []) {
    if (!documents || documents.length === 0) {
      return { insertedCount: 0 };
    }

    // 自动补齐 ID
    const safeIds = ids.length === documents.length 
      ? ids 
      : documents.map((_, i) => `doc-${Date.now()}-${i}`);

    const safeMetadatas = metadatas.length === documents.length
      ? metadatas
      : documents.map((_, i) => ({ index: i, timestamp: Date.now() }));

    try {
      if (typeof window !== 'undefined') {
        // 浏览器环境
        const res = await fetch('/api/seekdb/insert', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            ids: safeIds,
            documents,
            embeddings,
            metadatas: safeMetadatas
          })
        });

        if (!res.ok) {
          throw new Error(`批量插入 HTTP 异常: ${res.status}`);
        }

        const data = await res.json();
        if (!data.success) {
          throw new Error(data.message || '写入 SeekDB 失败');
        }

        console.log(`[DBService] ✅ 成功写入 ${documents.length} 条数据，并已显式完成 SeekDB 索引刷新！`);
        return data;
      } else {
        // Node.js 直调
        if (!this.nodeCollection) {
          await this.initCollection(this.collectionName, this.dimension);
        }

        // 写入数据
        await this.nodeCollection.add({
          ids: safeIds,
          documents,
          embeddings,
          metadatas: safeMetadatas
        });

        // 显式刷新索引
        await this.nodeCollection.refresh_index();
        return { success: true, insertedCount: documents.length, refreshed: true };
      }
    } catch (err) {
      console.error('[DBService] ❌ 批量写入或索引刷新失败:', err);
      throw new Error(`写入 SeekDB 失败: ${err.message}`);
    }
  }

  /**
   * 显式刷新索引方法
   */
  async refreshIndex() {
    try {
      if (typeof window !== 'undefined') {
        const res = await fetch('/api/seekdb/refresh', { method: 'POST' });
        return await res.json();
      } else {
        if (this.nodeCollection) {
          await this.nodeCollection.refresh_index();
        }
        return { success: true, message: 'Node.js collection refreshed' };
      }
    } catch (err) {
      console.error('[DBService] 刷新索引异常:', err);
      throw err;
    }
  }

  /**
   * 3. 原生复合混合检索 (Native Hybrid Search)
   * 
   * 核心红线语法规范:
   * 检索时，必须向 SeekDB 发送同时包含向量(knn)和全文关键词(query)的复合查询结构。
   * 由 SeekDB 内部引擎完成多路检索与 RRF 融合打分，严禁在前端各自检索后拼合数组！
   * 
   * @param {Object} options 检索选项
   * @param {string} options.queryText 用户自然语言关键词/查询串
   * @param {number[]} options.queryVector 端侧计算的 384 维归一化查询向量
   * @param {number} options.nResults 期望返回的最优结果数量 (Top-K)
   * @param {'hybrid' | 'vector' | 'fulltext'} options.mode 检索模式 (默认 'hybrid')
   * @returns {Promise<Object>} 结构化的融合检索结果及延迟统计
   */
  async hybridSearch(options = {}) {
    const {
      queryText,
      queryVector,
      nResults = 5,
      mode = 'hybrid'
    } = options;

    if (!queryText && (!queryVector || queryVector.length === 0)) {
      throw new Error('混合检索必须提供查询关键词或向量');
    }

    const startTime = performance.now();

    try {
      if (typeof window !== 'undefined') {
        // -------------------------------------------------------------
        // 浏览器环境: 发送给 SeekDB 服务执行复合混合查询
        // -------------------------------------------------------------
        const res = await fetch('/api/seekdb/hybrid-search', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            queryText: queryText || '',
            queryVector: queryVector || null,
            nResults,
            mode
          })
        });

        if (!res.ok) {
          throw new Error(`混合检索 HTTP 异常: ${res.status}`);
        }

        const json = await res.json();
        const latencyMs = json.latencyMs || (performance.now() - startTime);

        // 归一化解析检索返回结果集
        const rawData = json.data || {};
        const ids = rawData.ids?.[0] || [];
        const documents = rawData.documents?.[0] || [];
        const metadatas = rawData.metadatas?.[0] || [];
        const distances = rawData.distances?.[0] || [];
        const scores = rawData.scores?.[0] || [];

        const formattedResults = [];
        for (let i = 0; i < ids.length; i++) {
          formattedResults.push({
            id: ids[i],
            document: documents[i] || '',
            metadata: metadatas[i] || {},
            distance: distances[i] !== undefined ? distances[i] : null,
            score: scores[i] !== undefined ? scores[i] : (1.0 - (distances[i] || 0)),
            rank: i + 1,
          });
        }

        return {
          results: formattedResults,
          latencyMs: Math.round(latencyMs * 10) / 10,
          totalHits: formattedResults.length,
          mode: json.mode || 'seekdb-hybrid',
        };
      } else {
        // -------------------------------------------------------------
        // Node.js 直调: 使用 seekdb-js 的原生 collection.hybridSearch()
        // -------------------------------------------------------------
        if (!this.nodeCollection) {
          await this.initCollection(this.collectionName, this.dimension);
        }

        // 构造 SeekDB 原生复合 Hybrid 检索结构
        const searchOptions = {
          nResults,
          include: ['documents', 'metadatas', 'distances']
        };

        if (mode === 'hybrid') {
          // 复合多路分支: 全文关键词 + 密集向量 + RRF 融合
          searchOptions.query = { whereDocument: { $contains: queryText } };
          searchOptions.knn = { queryEmbeddings: [queryVector], nResults };
          searchOptions.rank = { rrf: { rankWindowSize: 60, rankConstant: 60 } };
        } else if (mode === 'vector') {
          searchOptions.knn = { queryEmbeddings: [queryVector], nResults };
        } else if (mode === 'fulltext') {
          searchOptions.query = { whereDocument: { $contains: queryText } };
        }

        const rawData = await this.nodeCollection.hybridSearch(searchOptions);
        const latencyMs = performance.now() - startTime;

        const ids = rawData.ids?.[0] || [];
        const documents = rawData.documents?.[0] || [];
        const metadatas = rawData.metadatas?.[0] || [];
        const distances = rawData.distances?.[0] || [];

        const formattedResults = ids.map((id, i) => ({
          id,
          document: documents[i] || '',
          metadata: metadatas[i] || {},
          distance: distances[i] !== undefined ? distances[i] : null,
          rank: i + 1
        }));

        return {
          results: formattedResults,
          latencyMs: Math.round(latencyMs * 10) / 10,
          totalHits: formattedResults.length,
          mode: 'seekdb-native'
        };
      }
    } catch (err) {
      console.error('[DBService] ❌ 原生混合检索执行失败:', err);
      throw new Error(`SeekDB 混合检索失败: ${err.message}`);
    }
  }

  /**
   * 获取数据库连接与集合状态
   */
  async getStatus() {
    try {
      if (typeof window !== 'undefined') {
        const res = await fetch('/api/seekdb/status');
        return await res.json();
      } else {
        const count = this.nodeCollection ? await this.nodeCollection.count() : 0;
        return { success: true, connected: true, count };
      }
    } catch (err) {
      return { success: false, error: err.message };
    }
  }
}

export const dbService = new DBService();
export default dbService;

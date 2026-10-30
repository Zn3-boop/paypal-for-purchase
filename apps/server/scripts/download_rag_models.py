"""
RAG模型预下载脚本 — 离线优先策略

运行此脚本将embedding和reranker模型下载到本地目录，
后续RAG服务启动时使用local_files_only=True，不再联网下载。

用法:
    python scripts/download_rag_models.py              # 下载全部
    python scripts/download_rag_models.py --embedding   # 仅下载embedding
    python scripts/download_rag_models.py --reranker    # 仅下载reranker
    python scripts/download_rag_models.py --check       # 检查已有模型状态
"""
from __future__ import annotations

import argparse
import os
import sys
import shutil

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
RERANKER_MODEL = "BAAI/bge-reranker-base"

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.path.join(SERVER_DIR, "models")
EMBEDDING_DIR = os.path.join(MODELS_DIR, "all-MiniLM-L6-v2")
RERANKER_DIR = os.path.join(MODELS_DIR, "bge-reranker-base")


def check_models() -> dict[str, bool]:
    embedding_ok = os.path.isdir(EMBEDDING_DIR) and bool(os.listdir(EMBEDDING_DIR))
    reranker_ok = os.path.isdir(RERANKER_DIR) and bool(os.listdir(RERANKER_DIR))
    return {"embedding": embedding_ok, "reranker": reranker_ok}


def download_embedding():
    print(f"[1/2] 下载 Embedding 模型: {EMBEDDING_MODEL}")
    print(f"       目标目录: {EMBEDDING_DIR}")
    os.makedirs(EMBEDDING_DIR, exist_ok=True)

    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(EMBEDDING_MODEL)
        model.save(EMBEDDING_DIR)
        print(f"       ✓ Embedding 模型已保存到 {EMBEDDING_DIR}")
    except ImportError:
        print("       ✗ sentence-transformers 未安装，请先: pip install sentence-transformers")
        sys.exit(1)
    except Exception as e:
        print(f"       ✗ 下载失败: {e}")
        sys.exit(1)


def download_reranker():
    print(f"[2/2] 下载 Reranker 模型: {RERANKER_MODEL}")
    print(f"       目标目录: {RERANKER_DIR}")
    os.makedirs(RERANKER_DIR, exist_ok=True)

    try:
        from sentence_transformers import CrossEncoder
        model = CrossEncoder(RERANKER_MODEL)
        model.save(RERANKER_DIR)
        print(f"       ✓ Reranker 模型已保存到 {RERANKER_DIR}")
    except ImportError:
        print("       ✗ sentence-transformers 未安装，请先: pip install sentence-transformers")
        sys.exit(1)
    except Exception as e:
        print(f"       ✗ 下载失败: {e}")
        sys.exit(1)


def print_env_hint():
    embed_path = EMBEDDING_DIR.replace("\\", "/")
    rerank_path = RERANKER_DIR.replace("\\", "/")
    print("\n" + "=" * 60)
    print("模型下载完成！请在 .env 中配置以下环境变量：")
    print("=" * 60)
    print(f"RAG_EMBEDDING_MODEL_PATH={embed_path}")
    print(f"RAG_RERANK_ENABLED=true")
    print(f"RAG_RERANK_MODEL_PATH={rerank_path}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="RAG模型预下载脚本")
    parser.add_argument("--embedding", action="store_true", help="仅下载embedding模型")
    parser.add_argument("--reranker", action="store_true", help="仅下载reranker模型")
    parser.add_argument("--check", action="store_true", help="检查已有模型状态")
    args = parser.parse_args()

    if args.check:
        status = check_models()
        print("RAG模型状态检查:")
        print(f"  Embedding ({EMBEDDING_DIR}): {'✓ 已就绪' if status['embedding'] else '✗ 未下载'}")
        print(f"  Reranker  ({RERANKER_DIR}): {'✓ 已就绪' if status['reranker'] else '✗ 未下载'}")
        return

    download_embed = not args.reranker
    download_rerank = not args.embedding

    if args.embedding:
        download_embed = True
        download_rerank = False
    if args.reranker:
        download_embed = False
        download_rerank = True

    if download_embed:
        download_embedding()
    if download_rerank:
        download_reranker()

    print_env_hint()


if __name__ == "__main__":
    main()
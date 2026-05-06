import os
import random
import re
import string
from datetime import datetime

from dotenv import load_dotenv
from openai import OpenAI
from pinecone import Pinecone

from qlink_chatbot.utils.logger_config import logger

load_dotenv()

pinecone_api = os.getenv("PINECONE_API") or os.getenv("PINECONE_API_KEY")
openai_api_key = os.getenv("OPENAI_API_KEY")
pine_client = Pinecone(api_key=pinecone_api) if pinecone_api else None
openai_client = OpenAI(api_key=openai_api_key) if openai_api_key else None
index = pine_client.Index("demo") if pine_client else None

def get_embedding(text:str):
    if not openai_client:
        raise RuntimeError("OPENAI_API_KEY is not configured.")
    response = openai_client.embeddings.create(
        input=text,
        model="text-embedding-3-small"
    )

    return response.data[0].embedding


pinecone_kb_namespace = "jaipurrugs_kb"

def _generate_id(length=7):
    chars = string.ascii_letters + string.digits
    return ''.join(random.choices(chars, k=length))

def fetch_kb(
    vector: list,
    top_k: int = 3
) -> list:
    """Pinecone util function to perform similarity search in the db."""
    try:
        if not index:
            logger.warning("[Pinecone] PINECONE_API/PINECONE_API_KEY is not configured.")
            return []

        result = index.query(
            namespace=pinecone_kb_namespace,
            vector=vector,
            top_k=top_k,
            include_metadata=True
        )

        return result.get("matches", [])
    
    except Exception as e:
        raise e
    
def upsert_kb(
    vector: list,
    text: str,
    doc_id: str,
    lable = "agent",
) -> None:
    """Pinecone Util Function to append new vector to the db."""
    try:
        if not index:
            logger.warning("[Pinecone] PINECONE_API/PINECONE_API_KEY is not configured.")
            return None
        index.upsert(
            namespace=pinecone_kb_namespace,
            vectors=[
                {
                    "id": doc_id,
                    "values": vector,
                    "metadata": {
                        "text": text, 
                        "lable": lable,
                        "created_at": datetime.now().isoformat()
                    }
                }
            ]
        )
    
    except Exception as e:
        raise e
    

async def fetch_similar_sessions(query: str, top_k: int = 3):
    try:
        vector = get_embedding(query)
        results = fetch_kb(vector, top_k)
        kb = []
        for r in results:
            if "metadata" in r:
                kb.append(f"Source: {r['metadata']['lable']}, Knowledge: [ {r['metadata']['text']} ]")

        logger.info(f"[Pinecone] fetched KB for {query} is {"\n".join(kb)}.")
        return "\n".join(kb)
    except Exception as e:
        logger.error(f"[Pinecone] Error occred query search: {e}")


async def fetch_records_with_metadata(query: str, top_k: int = 3):
    try:
        vector = get_embedding(query)
        results = fetch_kb(vector, top_k)

        kb = []
        for r in results:
            if "metadata" in r:
                kb.append({
                    "id": r["id"],
                    **r["metadata"]
                })

        logger.info(f"[Pinecone] fetched KB for {query} is {kb}.")
        return kb

    except Exception as e:
        logger.error(f"[Pinecone] Error occurred query search: {e}")
        return []


async def store_vector_summary(session_id: str, summary: str, lable = "agent"):
    try:
        vector = get_embedding(summary)
        doc_id = f"{session_id}_{_generate_id()}"
        upsert_kb(vector, summary, doc_id, lable=lable)
        logger.info(f"[Pinecone] Stored summary for session {session_id} in KB.")
    except Exception as e:
        logger.error(f"[Pinecone] Error storing vector summary: {e}")

    

def list_records_by_label(lable: str, namespace: str = pinecone_kb_namespace):
    """Fetch all records in the given namespace filtered by label (agent/general)."""
    try:
        if not index:
            logger.warning("[Pinecone] PINECONE_API/PINECONE_API_KEY is not configured.")
            return None
        meta_response = None
        response = list(index.list(namespace=namespace))
        if response:
            m_respose = index.fetch(
                ids=response[0], 
                namespace=namespace
            )

            vectors = m_respose.vectors

            meta_response = list( {
                "id": vid,
                "metadata": vec.metadata
            }
            for vid, vec in vectors.items())

            output = []
            for mr in meta_response:
                if mr["metadata"]["lable"] == lable:
                    output.append(mr)
                
            
        logger.info(f"[Pinecone] Found {len(output)} records for label '{lable}'.")
        return output if output else None
    except Exception as e:
        logger.error(f"[Pinecone] Error listing records for label {lable}: {e}")
        raise e
    
def get_record_by_id(record_id: str, namespace: str = pinecone_kb_namespace):
    """Fetch a specific record and its metadata from Pinecone."""
    try:
        if not index:
            logger.warning("[Pinecone] PINECONE_API/PINECONE_API_KEY is not configured.")
            return None
        m_response = index.fetch(ids=[record_id], namespace=namespace)
        vectors = m_response.vectors

        if not vectors or record_id not in vectors:
            logger.info(f"[Pinecone] No record found for ID: {record_id}")
            return None

        vec = vectors[record_id]
        record = {
            "id": record_id,
            "text": vec.metadata["text"],
            "created_at": str(vec.metadata["created_at"] )
        }

        logger.info(f"[Pinecone] Record fetched for ID: {record_id}")
        return record

    except Exception as e:
        logger.error(f"[Pinecone] Error fetching record {record_id}: {e}")

def delete_record_by_id(record_id: str, namespace: str = pinecone_kb_namespace):
    """Delete a record from Pinecone namespace by its ID."""
    try:
        if not index:
            logger.warning("[Pinecone] PINECONE_API/PINECONE_API_KEY is not configured.")
            return None
        index.delete(ids=[record_id], namespace=namespace)
        logger.info(f"[Pinecone] Record deleted successfully {record_id} from KB.")
    except Exception as e:
        logger.error(f"[Pinecone] Error deleting records: {e}")


def chunk_text(text: str, max_length: int = 1000, overlap: int = 100):
    """Smart text chunking that tries to preserve sentence boundaries.
    """
    # normalize spacing
    text = re.sub(r'\s+', ' ', text).strip()
    sentences = re.split(r'(?<=[.!?]) +', text)

    chunks = []
    current_chunk = ""

    for sentence in sentences:
        # if adding this sentence exceeds limit, push the chunk
        if len(current_chunk) + len(sentence) > max_length:
            chunks.append(current_chunk.strip())
            # start next chunk with overlap from the end of previous chunk
            overlap_text = current_chunk[-overlap:] if overlap < len(current_chunk) else current_chunk
            current_chunk = overlap_text + " " + sentence
        else:
            current_chunk += " " + sentence

    if current_chunk:
        chunks.append(current_chunk.strip())

    return chunks

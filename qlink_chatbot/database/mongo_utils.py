import os
import time
from datetime import datetime

from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient

from qlink_chatbot.utils.logger_config import logger

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client["JR"]
sessions_collection = db["users"]
whatsapp_sessions_collection = db["users_whatsapp"]
internal_collection = db["internals"]
agent_alerts = db["agent_alerts"]
agents_profile = db["agents"]
inventory_cache_collection = db["inventory_cache"]


def _get_sessions_collection(collection_name: str = "users"):
    if collection_name == "users_whatsapp":
        return whatsapp_sessions_collection
    return sessions_collection


def agent_login(emp_id: str, password: str):
    try:
        # find agent by emp_id
        agent = agents_profile.find_one({"emp_id": emp_id})

        if not agent:
            return {"success": False, "message": "Employee not found"}

        if agent.get("password") != password:
            return {"success": False, "message": "Invalid password"}

        return {
            "success": True,
            "data": {
                "emp_id": agent.get("emp_id"),
                "name": agent.get("name"),
                "category": agent.get("category"),
                "_id": str(agent.get("_id")),
            },
        }

    except Exception as e:
        logger.error("Agent login error", extra={"error": str(e)})
        return {"success": False, "message": "Server error"}

def save_message(
    session_id: str,
    role: str,
    content: str,
    collection_name: str = "users",
):
    """Append message to chat_history inside session document."""
    try:
        now = datetime.utcnow()
        message = {"role": role, "content": content, "timestamp": now}
        session_collection = _get_sessions_collection(collection_name=collection_name)
        session_collection.update_one(
            {"session_id": session_id},
            {
                "$push": {"chat_history": message},
                "$set": {"updated_at": now},
            },
            upsert=True,
        )
    except Exception as e:
        logger.error("Error occurred while saving message", extra={"error": e})
        raise e

def create_session(
    session_id: str,
    country_code: str,
    name: str,
    is_ai: bool = True,
    collection_name: str = "users",
):
    """Create a new user session."""
    try:
        now = datetime.utcnow()
        session_collection = _get_sessions_collection(collection_name=collection_name)
        session_collection.insert_one({
            "session_id": session_id,
            "country_code": country_code,
            "is_ai": is_ai,
            "created_at": now,
            "updated_at": now,
            "user_name": name,
            "chat_history": []
        })
        logger.info(f"Session created: {session_id}")
    except Exception as e:
        logger.error("Error creating session", extra={"error": e})
        raise e

def update_session_country(session_id: str, country_code: str):
    """Update the country code of an existing session."""
    try:
        now = datetime.utcnow()
        result = sessions_collection.update_one(
            {"session_id": session_id},
            {"$set": {"country_code": country_code, "updated_at": now}}
        )
        logger.info(f"Updated country_code for session {session_id}")
        return result.modified_count
    except Exception as e:
        logger.error("Error updating session country", extra={"error": e})
        raise e

def toggle_ai(session_id: str):
    """Toggle the is_ai status of a session."""
    try:
        session = sessions_collection.find_one({"session_id": session_id})
        if not session:
            return None
        new_status = not session.get("is_ai", True)
        sessions_collection.update_one(
            {"session_id": session_id},
            {"$set": {"is_ai": new_status, "updated_at": datetime.utcnow()}}
        )
        return new_status
    except Exception as e:
        logger.error("Error toggling AI status", extra={"error": e})
        raise e

def get_chat_history(session_id: str):
    """Fetch chat history of a session."""
    try:
        session = sessions_collection.find_one({"session_id": session_id}, {"_id": 0})
        if not session:
            return None
        return session.get("chat_history", [])
    except Exception as e:
        logger.error("Error fetching chat history", extra={"error": e})
        raise e

def get_all_sessions():
    """Return all sessions in the database."""
    try:
        sessions = list(sessions_collection.find())
        for s in sessions:
            s["_id"] = str(s["_id"])
        return sessions
    except Exception as e:
        logger.error("Error fetching all sessions", extra={"error": e})
        raise e

def get_session_by_id(session_id: str, collection_name: str = "users"):
    """Return a single session by session_id."""
    try:
        session_collection = _get_sessions_collection(collection_name=collection_name)
        session = session_collection.find_one({"session_id": session_id})
        if session:
            session["_id"] = str(session["_id"])
        return session
    except Exception as e:
        logger.error("Error fetching session by id", extra={"error": e})
        raise e


def save_user_name(session_id: str, name: str, collection_name: str = "users"):
    """Store or update the user's name in the session."""
    try:
        now = datetime.utcnow()
        session_collection = _get_sessions_collection(collection_name=collection_name)
        result = session_collection.update_one(
            {"session_id": session_id},
            {"$set": {"user_name": name, "updated_at": now}}
        )
        logger.info(f"Saved name for session {session_id}: {name}")
        return result.modified_count
    except Exception as e:
        logger.error("Error saving user name", extra={"error": e})
        raise e

def reset_is_ai_true(session_id: str):
    """Reset is ai feild to true."""
    try:
        now = datetime.utcnow()
        result = sessions_collection.update_one(
            {"session_id": session_id},
            {"$set": {"is_ai": True}}
        )
    except Exception as e:
        logger.error("Error reseting is ai feild", extra={"error": e})
        raise e

def save_previous_search(
    session_id: str,
    search_keyword: str,
    search_results: list,
    collection_name: str = "users",
):
    """Store the user's previous search results in the session.
    Only keeps the last 3 searches.
    
    search_results: List of product dicts returned from Jaipur Rugs API.
    """
    try:
        now = datetime.utcnow()
        session_collection = _get_sessions_collection(collection_name=collection_name)
        result = session_collection.update_one(
            {"session_id": session_id},
            {
                "$push": {
                    "previous_searches": {
                        "$each": [
                            {
                                "keyword": search_keyword,
                                "results": search_results,
                                "timestamp": now
                            }
                        ],
                        "$slice": -3  # Keep only the last 3 searches
                    }
                },
                "$set": {"updated_at": now}
            },
            upsert=True
        )
        logger.info(f"Saved previous search for session {session_id}: {search_keyword}")
        return result.modified_count
    except Exception as e:
        logger.error("Error saving previous search", extra={"error": e})
        raise e
    
def get_previous_search(session_id: str, collection_name: str = "users"):
    """Fetch the previous search results for a session.
    Returns an empty list if none exist.
    """
    try:
        session_collection = _get_sessions_collection(collection_name=collection_name)
        session = session_collection.find_one(
            {"session_id": session_id},
            {"_id": 0, "previous_searches": 1, "previous_search": 1}
        )
        if session and "previous_searches" in session:
            return session["previous_searches"]
        if session and "previous_search" in session:
            return session["previous_search"]
        return []
    except Exception as e:
        logger.error("Error fetching previous search", extra={"error": e})
        raise e
    
def user_name(session_id: str, collection_name: str = "users"):
    """Fetch the previous search results for a session.
    Returns an empty list if none exist.
    """
    try:
        session_collection = _get_sessions_collection(collection_name=collection_name)
        session = session_collection.find_one({"session_id": session_id}, {"_id": 0, "user_name": 1})
        if session and "user_name" in session:
            return session["user_name"]
        return ""
    except Exception as e:
        logger.error("Error fetching user name", extra={"error": e})
        raise e
    
def return_system_prompt():
    """Returns system prompt."""
    try:
        response = internal_collection.find_one({"category": "system_prompt"}, {"_id": 0})
        return response if response else None
    except Exception as e:
        logger.error("Error Fetching system prompt variables", extra={"error": e})
        raise e
    
def update_system_prompt(
        system_identity: str, 
        system_conversation_style: str, 
        system_product_display_format: str,
        system_others: str
    ):
    """Update all editable fields of the system prompt at once."""
    try:
        update_fields = {
            "system_identity": system_identity,
            "system_conversation_style": system_conversation_style,
            "system_product_display_format": system_product_display_format,
            "system_others": system_others
        }

        result = internal_collection.update_one(
            {"category": "system_prompt"},
            {"$set": update_fields},
            upsert=False
        )

        if result.modified_count > 0:
            logger.info("System prompt updated successfully.")
            return {"status": "success", "updated_fields": list(update_fields.keys())}
        else:
            logger.warning("No changes made to the system prompt.")
            return {"status": "no_change"}

    except Exception as e:
        logger.error("Error updating system prompt", extra={"error": e})
        raise e
    
def raise_alert(session_id: str, alert_body: str):
    """Raise alert when ai esclate query to the agent."""
    try:
        result = agent_alerts.insert_one(
            {
                "session_id": session_id,
                "alert": alert_body,
                "created_at": int(time.time())
            }
        )
    except Exception as e:
        logger.error(
            "Error raising agent alert.",
            extra={
                "error": e
            }
        )
        raise e

def list_all_alerts():
    """Util function to return all alerts."""
    try:
        result = list(agent_alerts.find())
        if not result:
            return None
        
        for i in result:
            i["_id"] = str(i["_id"])
        
        return result
        
    except Exception as e:
        logger.error(
            "Error fetching agent alerts",
            extra={"error": e}
        )
        raise e

def delete_alert_by_id(id: str):
    """Util function to delete alert by id."""
    try:
        result = agent_alerts.delete_one(
            {"_id": ObjectId(id)}
        )

        if result.deleted_count == 0:
            return False

        return True
    except Exception as e:
        logger.error(
            "Error Occured while deleting alert by ID",
            {"error":e}
        )
        raise e


def get_inventory_cache():
    """Get cached inventory data and last fetch timestamp."""
    try:
        return inventory_cache_collection.find_one({"_id": "inventory_master"})
    except Exception as e:
        logger.error("Error fetching inventory cache", extra={"error": e})
        return None


def save_inventory_cache(data: list):
    """Save/update inventory data with current UTC timestamp."""
    try:
        now = datetime.utcnow()
        inventory_cache_collection.update_one(
            {"_id": "inventory_master"},
            {"$set": {"data": data, "last_fetched": now}},
            upsert=True,
        )
        logger.info(f"Saved inventory cache with {len(data)} records at {now}")
    except Exception as e:
        logger.error("Error saving inventory cache", extra={"error": e})
        raise e


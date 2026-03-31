import asyncio
import json
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pymongo import MongoClient

from qlink_chatbot.agent.chat_agent import chat_agent
from qlink_chatbot.agent.summariser_agent import summariser_agent
from qlink_chatbot.database.mongo_utils import (
    create_session,
    get_session_by_id,
    save_message,
    update_session_country,
    reset_is_ai_true
)
from qlink_chatbot.database.pinecone_utils import store_vector_summary
from qlink_chatbot.utils.logger_config import logger

MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client["JR"]
sessions_collection = db["users"]

ws_router = APIRouter()

active_connections: dict[str, dict[str, any]] = {}
admin_connections: set[WebSocket] = set()


async def notify_admins():
    """Send active user list to all connected admin dashboards"""
    data = [{"session_id": sid} for sid, conn in active_connections.items() if conn.get("user")]
    for admin_ws in list(admin_connections):
        try:
            await admin_ws.send_json({"type": "active_users", "data": data})
        except:
            admin_connections.remove(admin_ws)


# ---------------- USER SOCKET ---------------- #
@ws_router.websocket("/ws/user/{session_id}/{country_code}/{name}")
async def user_ws(websocket: WebSocket, session_id: str, country_code: str, name:str):
    session_id = session_id.lower()
    await websocket.accept()
    logger.info(f"User connected: {session_id} - {name} from {country_code}")

    session = get_session_by_id(session_id=session_id)
    if not session:
        create_session(session_id=session_id, country_code=country_code, name=name, is_ai=True)
    elif session.get("country_code") != country_code:
        update_session_country(session_id=session_id, country_code=country_code)

    if session_id not in active_connections:
        active_connections[session_id] = {"user": websocket, "agents": [], "agent_msgs": []}
    else:
        active_connections[session_id]["user"] = websocket

    await notify_admins()

    try:
        while True:
            data = await websocket.receive_text()
            # logger.info(f"User ({session_id}) sent: {data}")

            msg = json.loads(data)
            if msg.get("type") == "typing":
                for a in active_connections[session_id]["agents"]:
                    await a.send_json({"type": "typing", "from": "user", "is_typing": msg.get("is_typing", False)})
                continue
            
            if msg.get("from") != "user":
                continue

            message = {
                "type": "message",
                "from": "user", 
                "content": msg.get("content")
            }
            save_message(session_id, "user", message["content"])

            session = get_session_by_id(session_id=session_id)
            is_ai = session.get("is_ai", False)
            agents = active_connections[session_id]["agents"]

            if is_ai:
                # Assistant starts typing
                for a in agents:
                    await a.send_json(message)
                    await a.send_json({"type": "typing", "from": "assistant", "is_typing": True})
                await websocket.send_json({"type": "typing", "from": "assistant", "is_typing": True})
                
                response = await chat_agent(
                    chat_history=session.get("chat_history", []),
                    user_message=message["content"],
                    session_id=session_id,
                    country_code=country_code,
                    client_ip=websocket.client.host if websocket.client else ""
                )

                # Assistant stops typing
                for a in agents:
                    await a.send_json({"type": "typing", "from": "assistant", "is_typing": False})
                await websocket.send_json({"type": "typing", "from": "assistant", "is_typing": False})

                ai_response = {
                    "type": "message",
                    "from": "assistant", 
                    "content": response or "Error generating response."
                }
                save_message(session_id, "assistant", ai_response["content"])

                await websocket.send_json(ai_response)
                for a in agents:
                    await a.send_json(ai_response)

            else:
                for a in agents:
                    await a.send_json(message)

    except WebSocketDisconnect:
        logger.info(f"User disconnected: {session_id}")
        agent_msgs = active_connections[session_id].get("agent_msgs", [])
        if agent_msgs:
            asyncio.create_task(process_agent_learning(session_id, agent_msgs))
        active_connections.pop(session_id, None)
        await notify_admins()
    except Exception as e:
        logger.error(f"User websocket error for {session_id}: {e}")
        active_connections.pop(session_id, None)
        await notify_admins()


# ---------------- AGENT SOCKET ---------------- #
@ws_router.websocket("/ws/agent/{session_id}/{emp_id}")
async def agent_ws(websocket: WebSocket, session_id: str, emp_id: str):
    session_id = session_id.lower()
    await websocket.accept()
    logger.info(f"Agent connected for session: {session_id} with emp id {emp_id}")

    if session_id not in active_connections:
        active_connections[session_id] = {"user": None, "agents": [], "agent_msgs": []}

    active_connections[session_id]["agents"].append(websocket)

    try:
        while True:
            data = await websocket.receive_text()
            # logger.info(f"Agent ({session_id}) sent: {data}")

            msg = json.loads(data)
            if msg.get("type") == "handshake":
                user_ws = active_connections[session_id].get("user")
                if user_ws:
                    await user_ws.send_json({
                        "type": "handshake", 
                        "from": "agent", 
                        "agent_name": msg.get("name"),
                        "emp_id": emp_id
                    })
                for a in active_connections[session_id]["agents"]:
                    if a != websocket:
                        await a.send_json({
                            "type": "handshake", 
                            "from": "agent", 
                            "agent_name": msg.get("name"),
                            "emp_id": emp_id
                        })
                continue
                

            if msg.get("type") == "typing":
                user_ws = active_connections[session_id].get("user")
                if user_ws:
                    await user_ws.send_json({"type": "typing", "from": "agent", "is_typing": msg.get("is_typing", False)})
                for a in active_connections[session_id]["agents"]:
                    if a != websocket:
                        await a.send_json({"type": "typing", "from": "agent", "is_typing": msg.get("is_typing", False)})
                continue
            
            if msg.get("from") != "agent":
                continue

            message = {
                "type": "message",
                "from": "agent",
                "content": msg.get("content")
            }
            save_message(session_id, "agent", message["content"])
            active_connections[session_id]["agent_msgs"].append(message["content"])

            user_ws = active_connections[session_id].get("user")
            if user_ws:
                await user_ws.send_json(message)

            for a in active_connections[session_id]["agents"]:
                if a != websocket:
                    await a.send_json(message)

    except WebSocketDisconnect:
        logger.info(f"Agent disconnected for session: {session_id}")
        reset_is_ai_true(session_id=session_id)
        if websocket in active_connections[session_id]["agents"]:
            active_connections[session_id]["agents"].remove(websocket)
    except Exception as e:
        logger.error(f"Agent websocket error for {session_id}: {e}")
        if session_id in active_connections and websocket in active_connections[session_id]["agents"]:
            active_connections[session_id]["agents"].remove(websocket)


# ---------------- ADMIN SOCKET ---------------- #
@ws_router.websocket("/ws/admin")
async def admin_ws(websocket: WebSocket):
    await websocket.accept()
    admin_connections.add(websocket)
    await notify_admins()
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        admin_connections.remove(websocket)
        logger.info("Admin disconnected")


@ws_router.get("/active_users")
def get_active_users():
    active_users = [
        {"session_id": sid}
        for sid, conn in active_connections.items()
        if conn.get("user") is not None
    ]
    return {"active_users": active_users}


# ---------------- BACKGROUND LEARNING TASK ---------------- #
async def process_agent_learning(session_id: str, agent_msgs: list[str]):
    session_id = session_id.lower()
    try:
        logger.info(f"Summarizing {len(agent_msgs)} agent messages for session {session_id}")
        summary = await summariser_agent(agent_msgs)
        summary = json.loads(summary)
        if summary.get("is_worth_storing", None) and summary.get("summary", ""):
            await store_vector_summary(
                session_id=session_id,
                summary=summary.get("summary")
            )
            logger.info(f"Stored summary for session {session_id}")
        else:
            logger.info(f"No meaningful summary for session {session_id}")
            return None
    except Exception as e:
        logger.error(f"Error during agent learning for {session_id}: {e}")
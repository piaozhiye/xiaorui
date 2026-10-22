import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
TAG = __name__


async def handleAbortMessage(conn: "ConnectionHandler"):
    conn.logger.bind(tag=TAG).info("Abort message received")
    # 设置成打断状态，会自动打断llm、tts任务
    conn.close_after_chat = False
    conn.client_abort = True
    # A stop during the answer must also cancel music waiting to resume.
    tts = getattr(conn, "tts", None)
    if tts is not None:
        tts.music_resume = None
        tts._music_resume_content_seen = False
    owner = getattr(conn, "session_coordinator", None)
    if owner:
        owner.complete(conn.sentence_id, False)
    conn.clear_queues()
    # 打断客户端说话状态
    await conn.websocket.send(
        json.dumps({"type": "tts", "state": "stop", "session_id": conn.session_id})
    )
    conn.clearSpeakStatus()
    conn.logger.bind(tag=TAG).info("Abort message received-end")

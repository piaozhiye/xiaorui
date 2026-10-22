"""Correlated HA evidence; no credentials or wire-protocol changes."""
import json


def accepted(conn, text, op, domain, entities, result):
    record = dict(sentence_id=conn.sentence_id,
                  session_id=getattr(conn, "session_id", ""), text=text,
                  op=op, domain=domain, entities=entities, result=result)
    conn._ha_audit = record
    conn.logger.bind(tag=__name__).info(
        "HA_AUDIT " + json.dumps(dict(record, phase="accepted"), ensure_ascii=False))


def completed(conn, sentence_id):
    record = getattr(conn, "_ha_audit", None)
    if not record or record["sentence_id"] != sentence_id:
        return
    conn._ha_audit = None
    flow = getattr(conn, "audio_flow_control", {}) or {}
    packets = flow.get("packet_count", 0) if flow.get("sentence_id") == sentence_id else 0
    conn.logger.bind(tag=__name__).info("HA_AUDIT " + json.dumps(dict(
        record, phase="completed", packets=packets,
        aborted=bool(getattr(conn, "client_abort", False))), ensure_ascii=False))

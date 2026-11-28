#!/usr/bin/env python3
"""Read-only long-play and reconnect acceptance monitor for the music UI API."""
import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request


def fetch_state(base_url, cookie):
    request = urllib.request.Request(base_url.rstrip("/") + "/api/state",
                                     headers={"Cookie": cookie, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=8) as response:
        data = json.loads(response.read())
    if not data.get("ok"):
        raise RuntimeError(data.get("error") or "state API returned an error")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Controller base URL, e.g. https://controller.example:8765")
    parser.add_argument("--cookie-file", help="file containing the Cookie header value")
    parser.add_argument("--players", required=True, help="Comma-separated Snapcast player IDs that must remain online")
    parser.add_argument("--minutes", type=float, default=60, help="Long-play observation duration (default: 60 minutes)")
    parser.add_argument("--expect-reconnect", help="One player ID to watch for offline -> online during the run")
    parser.add_argument("--reconnect-timeout", type=float, default=300,
                        help="Seconds allowed for the chosen player to reconnect")
    parser.add_argument("--interval", type=float, default=3, help="Polling interval in seconds")
    args = parser.parse_args()
    cookie = os.environ.get("XIAORUI_COOKIE")
    if args.cookie_file:
        try:
            cookie = pathlib.Path(args.cookie_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            parser.error(f"cannot read cookie file: {exc}")
    if not cookie:
        parser.error("set XIAORUI_COOKIE or pass --cookie-file")
    required = [value.strip() for value in args.players.split(",") if value.strip()]
    if not required or args.minutes <= 0 or args.interval <= 0:
        parser.error("players、minutes 和 interval 必须有效")
    if args.expect_reconnect and args.expect_reconnect not in required:
        parser.error("expect-reconnect 必须同时包含在 --players 中")
    start = time.monotonic()
    end = start + args.minutes * 60
    saw_offline = False
    watched_online = False
    reconnect_deadline = (start + args.reconnect_timeout) if args.expect_reconnect else None
    previous_segment = None
    previous_position = None
    position_advanced = False
    saw_file_track = False
    observations = 0
    while time.monotonic() < end:
        try:
            state = fetch_state(args.url, cookie)
        except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
            print(f"FAIL t={int(time.monotonic()-start)}s controller unavailable: {exc}", flush=True)
            return 1
        players = {row.get("id"): row for row in state.get("players", [])}
        offline = [pid for pid in required if not players.get(pid, {}).get("connected")
                   and pid != args.expect_reconnect]
        if args.expect_reconnect:
            watched_connected = players.get(args.expect_reconnect, {}).get("connected")
            if watched_connected and not saw_offline:
                watched_online = True
            if not watched_connected and watched_online:
                saw_offline = True
            elif watched_connected and saw_offline:
                args.expect_reconnect = None
                print(f"PASS reconnect observed at t={int(time.monotonic()-start)}s", flush=True)
            elif time.monotonic() > reconnect_deadline:
                print("FAIL reconnect transition not observed before timeout", flush=True)
                return 1
        queue = state.get("queue") or {}
        if queue.get("state") == "error":
            print(f"FAIL playback error: {queue.get('error')}", flush=True)
            return 1
        if queue.get("state") != "playing" or not queue.get("session_id"):
            print(f"FAIL playback is not active: state={queue.get('state')} error={queue.get('error')}", flush=True)
            return 1
        track = queue.get("current_track_id") or queue.get("station_id")
        session_id = queue.get("session_id")
        segment = (track, session_id)
        position = queue.get("position_seconds")
        if queue.get("source_kind") != "radio":
            saw_file_track = True
        if segment != previous_segment:
            previous_segment = segment
            previous_position = position
        elif position is not None and previous_position is not None:
            if position + 1.0 < previous_position:
                print(f"FAIL playback position moved backwards: {previous_position} -> {position}", flush=True)
                return 1
            if position - previous_position >= 0.5:
                position_advanced = True
            previous_position = position
        elif position is not None:
            previous_position = position
        warning = queue.get("warning")
        if warning and not (args.expect_reconnect and warning.startswith("等待设备重连：")):
            print(f"FAIL playback warning: {warning}", flush=True)
            return 1
        if offline:
            print(f"FAIL required player(s) offline: {','.join(offline)}", flush=True)
            return 1
        observations += 1
        print(f"OK t={int(time.monotonic()-start)}s state={queue.get('state')} "
              f"track={track or '-'} session={session_id or '-'} players={len(required)} "
              f"position={queue.get('position_seconds')}", flush=True)
        time.sleep(min(args.interval, max(0.1, end - time.monotonic())))
    if args.expect_reconnect:
        print("FAIL reconnect was required but no offline -> online transition was observed", flush=True)
        return 1
    if saw_file_track and not position_advanced:
        print("FAIL local track position did not advance during the observation", flush=True)
        return 1
    print(f"PASS long-play observation: {observations} samples over {args.minutes:g} minutes", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

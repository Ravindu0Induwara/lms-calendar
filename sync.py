#!/usr/bin/env python3
"""
LMS Timeline to Apple Calendar (.ics) Auto-Sync
Designed for Moodle LMS (https://lms.tech.sjp.ac.lk)
Fetches deadlines from the user's timeline and generates an RFC-5545 iCalendar feed with Apple Calendar alarms.
"""

import os
import sys
import re
import json
import time
import hashlib
from datetime import datetime, timezone
import requests

LMS_BASE_URL = os.environ.get("LMS_URL", "https://lms.tech.sjp.ac.lk").rstrip("/")
USERNAME = os.environ.get("LMS_USERNAME")
PASSWORD = os.environ.get("LMS_PASSWORD")
OUTPUT_FILE = os.environ.get("OUTPUT_PATH", "dist/deadlines.ics")
TIMEZONE_NAME = os.environ.get("TIMEZONE", "Asia/Colombo")


def escape_ical_text(text: str) -> str:
    """Escapes special characters in text according to RFC 5545."""
    if not text:
        return ""
    text = text.replace("\\", "\\\\")
    text = text.replace(";", "\\;")
    text = text.replace(",", "\\,")
    text = text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    return text


def format_utc_ical(epoch_seconds: int) -> str:
    """Converts unix timestamp to iCalendar UTC string: YYYYMMDDTHHMMSSZ."""
    dt = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


class LMSSession:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self.sesskey = None

    def login(self, username, password) -> bool:
        login_url = f"{self.base_url}/login/index.php"
        print(f"[*] Navigating to login page: {login_url}")
        resp = self.session.get(login_url, timeout=20)
        resp.raise_for_status()

        # Extract logintoken if present
        logintoken_match = re.search(r'name=["\']logintoken["\']\s+value=["\']([^"\']*)["\']', resp.text)
        logintoken = logintoken_match.group(1) if logintoken_match else ""

        payload = {
            "anchor": "",
            "username": username,
            "password": password,
            "rememberusername": "1"
        }
        if logintoken:
            payload["logintoken"] = logintoken

        print(f"[*] Submitting credentials for user: {username}")
        post_resp = self.session.post(login_url, data=payload, allow_redirects=True, timeout=25)
        post_resp.raise_for_status()

        # Check for login success
        if "login/index.php" in post_resp.url and "Invalid login" in post_resp.text:
            print("[!] Error: Invalid username or password.")
            return False

        if "loginerrors" in post_resp.text or "error/moodle/invalidlogin" in post_resp.text:
            print("[!] Error: Login failed with authentication error.")
            return False

        # Extract sesskey
        self._extract_sesskey(post_resp.text)

        # If sesskey not found yet, request dashboard (/my/)
        if not self.sesskey:
            dash_resp = self.session.get(f"{self.base_url}/my/", timeout=20)
            self._extract_sesskey(dash_resp.text)

        if not self.sesskey:
            print("[?] Warning: Could not locate Moodle sesskey, will attempt alternate methods.")
        else:
            print(f"[+] Login successful! Found sesskey: {self.sesskey[:4]}****")

        return True

    def _extract_sesskey(self, html: str):
        match = re.search(r'["\']sesskey["\']:\s*["\']([a-zA-Z0-9]+)["\']', html)
        if not match:
            match = re.search(r'sesskey=([a-zA-Z0-9]+)', html)
        if match:
            self.sesskey = match.group(1)

    def fetch_events_via_webservice(self):
        """Fetches events using Moodle's internal AJAX calendar service."""
        if not self.sesskey:
            return []

        # 1. Try core_calendar_get_calendar_upcoming_view (Moodle 3.5+)
        upcoming_url = f"{self.base_url}/lib/ajax/service.php?sesskey={self.sesskey}&info=core_calendar_get_calendar_upcoming_view"
        print("[*] Requesting upcoming deadlines from Moodle calendar service...")
        try:
            resp = self.session.post(upcoming_url, json=[{"index": 0, "methodname": "core_calendar_get_calendar_upcoming_view", "args": {"courseid": 1}}], timeout=20)
            if resp.status_code == 200:
                res_json = resp.json()
                if isinstance(res_json, list) and len(res_json) > 0 and not res_json[0].get("error"):
                    raw_events = res_json[0].get("data", {}).get("events", [])
                    print(f"[+] Retrieved {len(raw_events)} events from upcoming view.")
                    parsed = []
                    for ev in raw_events:
                        title = ev.get("name", "Deadline")
                        course_info = ev.get("course", {})
                        course_name = course_info.get("fullname", "") or course_info.get("shortname", "")
                        due_time = ev.get("timestart") or ev.get("timesort")
                        url = ev.get("url") or (ev.get("action", {}).get("url") if isinstance(ev.get("action"), dict) else "")
                        event_id = str(ev.get("id", ""))
                        description = ev.get("description", "")
                        parsed.append({
                            "id": event_id,
                            "title": title,
                            "course": course_name,
                            "due_time": due_time,
                            "url": url,
                            "description": description
                        })
                    if parsed:
                        return parsed
        except Exception as e:
            print(f"[!] Upcoming view fetch note: {e}")

        # 2. Try core_calendar_get_action_events_by_timesort (Moodle 3.6+)
        ajax_url = f"{self.base_url}/lib/ajax/service.php?sesskey={self.sesskey}&info=core_calendar_get_action_events_by_timesort"
        now = int(time.time())
        time_from = now - (14 * 86400)
        time_to = now + (120 * 86400)
        payload = [
            {
                "index": 0,
                "methodname": "core_calendar_get_action_events_by_timesort",
                "args": {
                    "timesortfrom": time_from,
                    "timesortto": time_to,
                    "limit": 100
                }
            }
        ]

        try:
            resp = self.session.post(ajax_url, json=payload, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0 and not data[0].get("error"):
                    result = data[0]
                    if "data" in result and "events" in result["data"]:
                        raw_events = result["data"]["events"]
                        print(f"[+] Retrieved {len(raw_events)} events from timeline WebService.")
                        parsed = []
                        for ev in raw_events:
                            title = ev.get("name", "Deadline")
                            course_info = ev.get("course", {})
                            course_name = course_info.get("fullname", "") or course_info.get("shortname", "")
                            due_time = ev.get("timesort") or ev.get("timestart")
                            url = ev.get("url") or (ev.get("action", {}).get("url") if isinstance(ev.get("action"), dict) else "")
                            event_id = str(ev.get("id", ""))
                            description = ev.get("description", "")
                            parsed.append({
                                "id": event_id,
                                "title": title,
                                "course": course_name,
                                "due_time": due_time,
                                "url": url,
                                "description": description
                            })
                        if parsed:
                            return parsed
        except Exception as e:
            print(f"[!] Timesort fetch note: {e}")

        return []

    def fetch_events_via_html_scrape(self):
        """Scrapes timeline events directly from /my/ page HTML as fallback."""
        print("[*] Scraping /my/ dashboard HTML for timeline items...")
        resp = self.session.get(f"{self.base_url}/my/", timeout=20)
        html = resp.text
        
        events = []
        # Find all event items matching data-region="event-list-item"
        items = re.findall(
            r'<li[^>]*data-region=["\']event-list-item["\'][^>]*>(.*?)</li>',
            html,
            re.DOTALL
        )
        for item in items:
            title_match = re.search(r'<a[^>]*class=["\'][^"\']*event-name[^"\']*["\'][^>]*href=["\']([^"\']+)["\'][^>]*>([^<]+)</a>', item)
            course_match = re.search(r'<p[^>]*class=["\'][^"\']*text-muted[^"\']*["\'][^>]*>([^<]+)</p>', item)
            
            if title_match:
                url = title_match.group(1).strip()
                title = title_match.group(2).strip()
                course = course_match.group(1).strip() if course_match else ""
                
                # Check for assignment ID in URL
                id_match = re.search(r'id=(\d+)', url)
                event_id = id_match.group(1) if id_match else hashlib.md5((title + course).encode()).hexdigest()[:10]
                
                # Date extraction fallback
                # e.g., "19 Nov, 00:00" or similar
                date_text_match = re.search(r'(\d{1,2}\s+[A-Za-z]{3}(?:,\s+\d{2}:\d{2})?)', item)
                date_str = date_text_match.group(1) if date_text_match else ""
                
                events.append({
                    "id": event_id,
                    "title": title,
                    "course": course,
                    "url": url,
                    "raw_date": date_str,
                    "due_time": None
                })
        print(f"[+] Found {len(events)} events via HTML parsing.")
        return events


def build_ical(events: list) -> str:
    """Generates standard iCalendar (RFC 5545) with embedded Apple Calendar alerts."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//University of Sri Jayewardenepura//LMS Deadlines Sync//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:LMS Deadlines",
        f"X-WR-TIMEZONE:{TIMEZONE_NAME}",
        "X-WR-CALDESC:Auto-synced academic deadlines from lms.tech.sjp.ac.lk",
    ]

    now_utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    for ev in events:
        due_time = ev.get("due_time")
        if not due_time:
            continue

        dt_str = format_utc_ical(due_time)
        uid = f"moodle-deadline-{ev['id']}@lms.tech.sjp.ac.lk"
        
        course_prefix = f"[{ev['course'].split()[0]}] " if ev.get("course") else ""
        summary = f"{course_prefix}{ev['title']}"
        
        desc_parts = []
        if ev.get("course"):
            desc_parts.append(f"Course: {ev['course']}")
        if ev.get("url"):
            desc_parts.append(f"Submission Link: {ev['url']}")
        if ev.get("description"):
            # Strip simple html tags
            clean_desc = re.sub(r'<[^>]+>', '', ev["description"]).strip()
            if clean_desc:
                desc_parts.append(clean_desc)
        description = "\n".join(desc_parts)

        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now_utc}",
            f"DTSTART:{dt_str}",
            f"DTEND:{dt_str}",
            f"SUMMARY:{escape_ical_text(summary)}",
            f"DESCRIPTION:{escape_ical_text(description)}",
            f"URL:{ev.get('url', '')}",
            "STATUS:CONFIRMED",
            "TRANSP:OPAQUE",
            # Alarm 1: 24 hours prior
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:Deadline in 24 hours: {escape_ical_text(summary)}",
            "TRIGGER:-P1D",
            "END:VALARM",
            # Alarm 2: 2 hours prior
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:Deadline in 2 hours: {escape_ical_text(summary)}",
            "TRIGGER:-PT2H",
            "END:VALARM",
            "END:VEVENT"
        ])

    lines.append("END:VCALENDAR")
    lines.append("")  # trailing newline
    return "\r\n".join(lines)


def main():
    if not USERNAME or not PASSWORD:
        print("[!] Missing credentials. Please set LMS_USERNAME and LMS_PASSWORD environment variables.")
        sys.exit(1)

    print(f"[*] Starting LMS Calendar Sync for {LMS_BASE_URL}...")
    lms = LMSSession(LMS_BASE_URL)
    if not lms.login(USERNAME, PASSWORD):
        print("[!] Could not log in. Aborting sync.")
        sys.exit(1)

    # 1. Try timeline web service
    events = lms.fetch_events_via_webservice()

    # 2. Fallback to HTML scrape if needed
    if not events:
        events = lms.fetch_events_via_html_scrape()

    if not events:
        print("[*] No upcoming deadlines found on timeline. Generating empty calendar feed.")
    else:
        print(f"[+] Total deadlines processed: {len(events)}")
        for e in events:
            time_str = datetime.fromtimestamp(e['due_time'], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if e.get('due_time') else 'Unknown'
            print(f"    - {e.get('course', 'General')}: {e['title']} (Due: {time_str})")

    # Generate iCalendar
    ical_content = build_ical(events)

    # Ensure output directory exists
    os.makedirs(os.path.dirname(os.path.abspath(OUTPUT_FILE)), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(ical_content)

    print(f"[+] Successfully wrote calendar feed to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
